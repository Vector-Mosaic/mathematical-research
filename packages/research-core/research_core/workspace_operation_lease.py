"""One reusable kernel-backed lease primitive for workspace operations.

The lock file carries safe reconciliation metadata, but its existence is never
authority.  Exclusivity comes from the live descriptor-held kernel lock.  A
``HeldKernelLease`` is therefore usable only while its issuing context remains
entered; a lease id by itself is not proof of ownership.
"""

from __future__ import annotations

import errno
import hashlib
import os
import stat
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator, Mapping

from .json_support import canonical_json_bytes, loads_strict_json_bytes


class KernelLeaseError(RuntimeError):
    """Closed failure while acquiring or validating one kernel lease."""

    def __init__(self, code: str, message: str, *, path: Path) -> None:
        super().__init__(message)
        self.code = code
        self.path = path


@dataclass(frozen=True, slots=True)
class ReleasedKernelLeaseVerification:
    """Read-only evidence that one exact persisted lease is released and unlocked."""

    path: Path
    format_name: str
    lease_id: str
    scope_sha256: str
    acquired_at: str
    released_at: str
    metadata_sha256: str


@dataclass(slots=True)
class _LeaseState:
    descriptor: int
    device: int
    inode: int
    held: bool = True


_LEASE_ISSUER = object()
_RESERVED_METADATA_KEYS = frozenset(
    {
        "acquired_at",
        "format",
        "lease_id",
        "lifecycle",
        "predecessor_abandoned",
        "predecessor_lease_id",
        "released_at",
    }
)


class HeldKernelLease:
    """Opaque proof that one specific descriptor is still kernel-locked."""

    __slots__ = (
        "_issuer",
        "_lease_id",
        "_path",
        "_predecessor_lease_id",
        "_predecessor_scope_sha256",
        "_prior_abandoned",
        "_scope_sha256",
        "_state",
    )

    def __init__(
        self,
        *,
        path: Path,
        lease_id: str,
        predecessor_lease_id: str | None,
        predecessor_scope_sha256: str | None,
        prior_abandoned: bool,
        scope_sha256: str,
        state: _LeaseState,
        issuer: object,
    ) -> None:
        if issuer is not _LEASE_ISSUER:
            raise TypeError("HeldKernelLease values are issued only by acquire_kernel_lease")
        object.__setattr__(self, "_issuer", issuer)
        object.__setattr__(self, "_state", state)
        object.__setattr__(self, "_path", path)
        object.__setattr__(self, "_lease_id", lease_id)
        object.__setattr__(self, "_predecessor_lease_id", predecessor_lease_id)
        object.__setattr__(
            self,
            "_predecessor_scope_sha256",
            predecessor_scope_sha256,
        )
        object.__setattr__(self, "_prior_abandoned", prior_abandoned)
        object.__setattr__(self, "_scope_sha256", scope_sha256)

    @property
    def path(self) -> Path:
        return self._path

    @property
    def lease_id(self) -> str:
        return self._lease_id

    @property
    def prior_abandoned(self) -> bool:
        return self._prior_abandoned

    @property
    def predecessor_lease_id(self) -> str | None:
        """Return the persisted predecessor identity as diagnostic binding."""

        return self._predecessor_lease_id

    @property
    def predecessor_scope_sha256(self) -> str | None:
        """Return the predecessor scope digest only when its metadata was exact."""

        return self._predecessor_scope_sha256

    @property
    def scope_sha256(self) -> str:
        return self._scope_sha256

    def assert_held(self) -> None:
        """Fail unless the original locked descriptor is still live."""

        if self._issuer is not _LEASE_ISSUER or not self._state.held:
            raise KernelLeaseError(
                "kernel_lease_not_held",
                "kernel lease is no longer held",
                path=self.path,
            )
        try:
            current = os.fstat(self._state.descriptor)
        except OSError as exc:
            raise KernelLeaseError(
                "kernel_lease_not_held",
                "kernel lease descriptor is no longer live",
                path=self.path,
            ) from exc
        if current.st_dev != self._state.device or current.st_ino != self._state.inode:
            raise KernelLeaseError(
                "kernel_lease_not_held",
                "kernel lease descriptor identity changed",
                path=self.path,
            )


def _now() -> str:
    return (
        datetime.now(timezone.utc)
        .isoformat(timespec="microseconds")
        .replace("+00:00", "Z")
    )


def _fsync_directory(path: Path) -> None:
    if os.name == "nt":
        return
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _lock_descriptor(descriptor: int) -> None:
    os.lseek(descriptor, 0, os.SEEK_SET)
    if os.name == "nt":
        import msvcrt

        msvcrt.locking(descriptor, msvcrt.LK_NBLCK, 1)
    else:
        import fcntl

        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)


def _unlock_descriptor(descriptor: int) -> None:
    os.lseek(descriptor, 0, os.SEEK_SET)
    if os.name == "nt":
        import msvcrt

        msvcrt.locking(descriptor, msvcrt.LK_UNLCK, 1)
    else:
        import fcntl

        fcntl.flock(descriptor, fcntl.LOCK_UN)


def _parse_lease_timestamp(value: object, *, label: str, path: Path) -> datetime:
    if not isinstance(value, str) or not value.endswith("Z"):
        raise KernelLeaseError(
            "kernel_lease_released_metadata_invalid",
            f"kernel lease {label} must be one UTC timestamp",
            path=path,
        )
    try:
        parsed = datetime.fromisoformat(value[:-1] + "+00:00")
    except ValueError as exc:
        raise KernelLeaseError(
            "kernel_lease_released_metadata_invalid",
            f"kernel lease {label} is not a valid timestamp",
            path=path,
        ) from exc
    if parsed.tzinfo is None or parsed.utcoffset() != timezone.utc.utcoffset(parsed):
        raise KernelLeaseError(
            "kernel_lease_released_metadata_invalid",
            f"kernel lease {label} must be UTC",
            path=path,
        )
    normalized = parsed.astimezone(timezone.utc)
    if value != normalized.isoformat(timespec="microseconds").replace("+00:00", "Z"):
        raise KernelLeaseError(
            "kernel_lease_released_metadata_invalid",
            f"kernel lease {label} is not in canonical UTC form",
            path=path,
        )
    return normalized


def verify_released_kernel_lease(
    lock_path: str | os.PathLike[str],
    *,
    format_name: str,
    scope: Mapping[str, Any],
    expected_lease_id: str,
    require_pristine_predecessor: bool = False,
) -> ReleasedKernelLeaseVerification:
    """Verify one released lease without acquiring a lease or rewriting its file.

    The function takes a temporary nonblocking kernel lock only to prove that no
    executor currently owns the descriptor and to make the metadata read stable.
    It never invokes :func:`acquire_kernel_lease` and never writes the file.
    """

    path = Path(lock_path)
    if not path.is_absolute() or not path.parent.is_dir():
        raise KernelLeaseError(
            "kernel_lease_unsafe",
            "released kernel lease path and parent must be absolute and present",
            path=path,
        )
    if path.is_symlink() or not path.is_file():
        raise KernelLeaseError(
            "kernel_lease_unsafe",
            "released kernel lease must be one existing plain file",
            path=path,
        )
    if not isinstance(format_name, str) or not format_name.strip():
        raise ValueError("kernel lease format_name must be non-empty")
    overlap = _RESERVED_METADATA_KEYS.intersection(scope)
    if overlap:
        raise ValueError(
            "kernel lease scope uses reserved metadata keys: "
            + ", ".join(sorted(overlap))
        )
    normalized_scope = dict(scope)
    if any(not isinstance(key, str) or not key for key in normalized_scope):
        raise ValueError("kernel lease scope keys must be non-empty strings")
    scope_sha256 = hashlib.sha256(canonical_json_bytes(normalized_scope)).hexdigest()
    if not valid_kernel_lease_id(expected_lease_id):
        raise ValueError("expected kernel lease id is invalid")

    flags = os.O_RDONLY
    if hasattr(os, "O_CLOEXEC"):
        flags |= os.O_CLOEXEC
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise KernelLeaseError(
            "kernel_lease_unsafe",
            "released kernel lease could not be opened safely",
            path=path,
        ) from exc
    locked = False
    try:
        try:
            _lock_descriptor(descriptor)
            locked = True
        except OSError as exc:
            if exc.errno in {errno.EACCES, errno.EAGAIN, errno.EDEADLK}:
                raise KernelLeaseError(
                    "kernel_lease_busy",
                    "kernel lease is still held by an executor",
                    path=path,
                ) from exc
            raise

        stat_result = os.fstat(descriptor)
        if stat_result.st_size <= 0 or stat_result.st_size > 1024 * 1024:
            raise KernelLeaseError(
                "kernel_lease_released_metadata_invalid",
                "released kernel lease metadata size is invalid",
                path=path,
            )
        os.lseek(descriptor, 0, os.SEEK_SET)
        raw = os.read(descriptor, stat_result.st_size)
        try:
            metadata = loads_strict_json_bytes(raw)
        except Exception as exc:
            raise KernelLeaseError(
                "kernel_lease_released_metadata_invalid",
                "released kernel lease metadata is not exact JSON",
                path=path,
            ) from exc
        expected_keys = _RESERVED_METADATA_KEYS | frozenset(normalized_scope)
        if not isinstance(metadata, dict) or set(metadata) != expected_keys:
            raise KernelLeaseError(
                "kernel_lease_released_metadata_invalid",
                "released kernel lease metadata fields are not exact",
                path=path,
            )
        if (
            metadata.get("format") != format_name.strip()
            or metadata.get("lease_id") != expected_lease_id
            or metadata.get("lifecycle") != "released"
            or any(metadata.get(key) != value for key, value in normalized_scope.items())
        ):
            raise KernelLeaseError(
                "kernel_lease_released_metadata_invalid",
                "released kernel lease format, scope, id, or lifecycle differs",
                path=path,
            )
        if not isinstance(metadata.get("predecessor_abandoned"), bool):
            raise KernelLeaseError(
                "kernel_lease_released_metadata_invalid",
                "released kernel lease predecessor state is invalid",
                path=path,
            )
        predecessor_id = metadata.get("predecessor_lease_id")
        if predecessor_id is not None and not valid_kernel_lease_id(
            predecessor_id
        ):
            raise KernelLeaseError(
                "kernel_lease_released_metadata_invalid",
                "released kernel lease predecessor id is invalid",
                path=path,
            )
        if require_pristine_predecessor and (
            metadata["predecessor_abandoned"] is not False or predecessor_id is not None
        ):
            raise KernelLeaseError(
                "kernel_lease_released_metadata_invalid",
                "first-root lease must have no predecessor or abandoned owner",
                path=path,
            )
        acquired = _parse_lease_timestamp(
            metadata.get("acquired_at"), label="acquired_at", path=path
        )
        released = _parse_lease_timestamp(
            metadata.get("released_at"), label="released_at", path=path
        )
        if released < acquired:
            raise KernelLeaseError(
                "kernel_lease_released_metadata_invalid",
                "released kernel lease predates acquisition",
                path=path,
            )
        return ReleasedKernelLeaseVerification(
            path=path,
            format_name=format_name.strip(),
            lease_id=expected_lease_id,
            scope_sha256=scope_sha256,
            acquired_at=str(metadata["acquired_at"]),
            released_at=str(metadata["released_at"]),
            metadata_sha256=hashlib.sha256(raw).hexdigest(),
        )
    finally:
        if locked:
            _unlock_descriptor(descriptor)
        os.close(descriptor)


def _write_lock_metadata(descriptor: int, payload: Mapping[str, Any]) -> None:
    raw = canonical_json_bytes(payload)
    os.lseek(descriptor, 0, os.SEEK_SET)
    os.write(descriptor, raw)
    os.ftruncate(descriptor, len(raw))
    os.fsync(descriptor)


def valid_kernel_lease_id(value: object) -> bool:
    return (
        isinstance(value, str)
        and value.startswith("lease-")
        and len(value) == 38
        and all(character in "0123456789abcdef" for character in value[6:])
    )


def _exact_predecessor_scope_sha256(
    metadata: object,
    *,
    path: Path,
    format_name: str,
    normalized_scope: Mapping[str, Any],
    scope_sha256: str,
) -> str | None:
    """Recognize an exact predecessor scope without treating metadata as a lease."""

    if not isinstance(metadata, dict):
        return None
    expected_keys = _RESERVED_METADATA_KEYS | frozenset(normalized_scope)
    if (
        set(metadata) != expected_keys
        or metadata.get("format") != format_name
        or metadata.get("lifecycle") != "held"
        or not valid_kernel_lease_id(metadata.get("lease_id"))
        or metadata.get("released_at") is not None
        or type(metadata.get("predecessor_abandoned")) is not bool
        or (
            metadata.get("predecessor_lease_id") is not None
            and not valid_kernel_lease_id(metadata.get("predecessor_lease_id"))
        )
        or any(metadata.get(key) != value for key, value in normalized_scope.items())
    ):
        return None
    try:
        _parse_lease_timestamp(
            metadata.get("acquired_at"),
            label="acquired_at",
            path=path,
        )
    except KernelLeaseError:
        return None
    return scope_sha256


def _assert_plain_lock_descriptor(
    descriptor: int,
    path: Path,
) -> os.stat_result:
    """Bind the locked descriptor to one independent regular path entry."""

    try:
        opened = os.fstat(descriptor)
        linked = os.lstat(path)
    except OSError as exc:
        raise KernelLeaseError(
            "kernel_lease_unsafe",
            "kernel lease path identity cannot be authenticated",
            path=path,
        ) from exc
    if (
        not stat.S_ISREG(opened.st_mode)
        or not stat.S_ISREG(linked.st_mode)
        or int(opened.st_nlink) != 1
        or int(linked.st_nlink) != 1
        or (opened.st_dev, opened.st_ino) != (linked.st_dev, linked.st_ino)
    ):
        raise KernelLeaseError(
            "kernel_lease_unsafe",
            "kernel lease must be one independent regular file",
            path=path,
        )
    return opened


@contextmanager
def acquire_kernel_lease(
    lock_path: str | os.PathLike[str],
    *,
    format_name: str,
    scope: Mapping[str, Any],
) -> Iterator[HeldKernelLease]:
    """Acquire one nonblocking descriptor-held lease and persist safe metadata."""

    path = Path(lock_path)
    if not path.is_absolute():
        raise KernelLeaseError(
            "kernel_lease_unsafe",
            "kernel lease path must be absolute",
            path=path,
        )
    if not path.parent.is_dir():
        raise KernelLeaseError(
            "kernel_lease_unsafe",
            "kernel lease parent must already be a directory",
            path=path,
        )
    if path.is_symlink():
        raise KernelLeaseError(
            "kernel_lease_unsafe",
            "kernel lease path is a symlink",
            path=path,
        )
    if not isinstance(format_name, str) or not format_name.strip():
        raise ValueError("kernel lease format_name must be non-empty")
    overlap = _RESERVED_METADATA_KEYS.intersection(scope)
    if overlap:
        raise ValueError(
            "kernel lease scope uses reserved metadata keys: "
            + ", ".join(sorted(overlap))
        )
    normalized_scope = dict(scope)
    scope_sha256 = hashlib.sha256(canonical_json_bytes(normalized_scope)).hexdigest()

    try:
        descriptor = os.open(
            path,
            os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0),
            0o600,
        )
    except OSError as exc:
        raise KernelLeaseError(
            "kernel_lease_unsafe",
            "kernel lease path could not be opened without following links",
            path=path,
        ) from exc
    locked = False
    state: _LeaseState | None = None
    try:
        _assert_plain_lock_descriptor(descriptor, path)
        if os.fstat(descriptor).st_size == 0:
            os.write(descriptor, b"\0")
            os.fsync(descriptor)
            _fsync_directory(path.parent)
        try:
            _lock_descriptor(descriptor)
            locked = True
        except OSError as exc:
            if exc.errno in {errno.EACCES, errno.EAGAIN, errno.EDEADLK}:
                raise KernelLeaseError(
                    "kernel_lease_busy",
                    "kernel lease is already held by another executor",
                    path=path,
                ) from exc
            raise

        # Close the check/open and lock-acquisition substitution windows before
        # reading or replacing persisted lease metadata.
        _assert_plain_lock_descriptor(descriptor, path)

        prior_abandoned = False
        prior_lease_id: str | None = None
        prior_scope_sha256: str | None = None
        prior_raw = b""
        try:
            os.lseek(descriptor, 0, os.SEEK_SET)
            prior_raw = os.read(descriptor, max(os.fstat(descriptor).st_size, 1))
            prior = loads_strict_json_bytes(prior_raw)
            if isinstance(prior, dict):
                observed_prior_id = prior.get("lease_id")
                observed_prior_lease_id = (
                    str(observed_prior_id)
                    if valid_kernel_lease_id(observed_prior_id)
                    else None
                )
                prior_abandoned = prior.get("lifecycle") == "held"
                prior_scope_sha256 = _exact_predecessor_scope_sha256(
                    prior,
                    path=path,
                    format_name=format_name.strip(),
                    normalized_scope=normalized_scope,
                    scope_sha256=scope_sha256,
                )
                if (
                    prior_abandoned
                    and prior_scope_sha256 == scope_sha256
                    and observed_prior_lease_id is not None
                ):
                    inherited_owner = prior.get("predecessor_lease_id")
                    prior_lease_id = (
                        str(inherited_owner)
                        if prior.get("predecessor_abandoned") is True
                        and valid_kernel_lease_id(inherited_owner)
                        else observed_prior_lease_id
                    )
        except Exception:
            prior_abandoned = prior_raw not in {b"", b"\0"}

        lease_id = f"lease-{uuid.uuid4().hex}"
        held = {
            "format": format_name.strip(),
            **normalized_scope,
            "lease_id": lease_id,
            "acquired_at": _now(),
            "released_at": None,
            "lifecycle": "held",
            "predecessor_lease_id": prior_lease_id,
            "predecessor_abandoned": prior_abandoned,
        }
        _write_lock_metadata(descriptor, held)
        stat_result = os.fstat(descriptor)
        state = _LeaseState(
            descriptor=descriptor,
            device=stat_result.st_dev,
            inode=stat_result.st_ino,
        )
        lease = HeldKernelLease(
            path=path,
            lease_id=lease_id,
            predecessor_lease_id=prior_lease_id,
            predecessor_scope_sha256=prior_scope_sha256,
            prior_abandoned=prior_abandoned,
            scope_sha256=scope_sha256,
            state=state,
            issuer=_LEASE_ISSUER,
        )
        try:
            yield lease
        finally:
            released = {**held, "released_at": _now(), "lifecycle": "released"}
            try:
                _write_lock_metadata(descriptor, released)
            finally:
                state.held = False
                _unlock_descriptor(descriptor)
                locked = False
    finally:
        if state is not None:
            state.held = False
        if locked:
            try:
                _unlock_descriptor(descriptor)
            except OSError:
                pass
        os.close(descriptor)


__all__ = [
    "HeldKernelLease",
    "KernelLeaseError",
    "ReleasedKernelLeaseVerification",
    "acquire_kernel_lease",
    "verify_released_kernel_lease",
]
