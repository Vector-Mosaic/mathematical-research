"""Validated, host-local paths for the inactive research workspace.

The workspace is deliberately outside the repository and outside common sync
roots.  Merely importing this module, asking for the default path, or creating
a :class:`WorkspacePaths` value never creates a directory.
"""

from __future__ import annotations

import hashlib
import hmac
import os
import re
import secrets
import stat
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Any, Mapping

from .json_support import canonical_json_bytes


class WorkspacePathError(ValueError):
    """Raised when a workspace root or logical path violates containment."""


_SYNC_COMPONENTS = frozenset(
    {
        "box",
        "box sync",
        "dropbox",
        "google drive",
        "googledrive",
        "icloud drive",
        "icloudrive",
        "onedrive",
        "syncthing",
    }
)
_URI_OR_NETWORK_PREFIX = re.compile(
    r"^(?:[a-z][a-z0-9+.-]*://|smb:|nfs:|afp:)", re.IGNORECASE
)
_ATTESTATION_ISSUER = object()
_ATTESTATION_SECRET = secrets.token_bytes(32)


def _platform_family(platform_name: str | None) -> str:
    value = (platform_name or sys.platform).casefold()
    if value.startswith("win"):
        return "windows"
    if value.startswith("linux"):
        return "linux"
    raise WorkspacePathError(f"unsupported workspace platform: {platform_name or sys.platform}")


def default_workspace_root(
    *,
    platform_name: str | None = None,
    environ: Mapping[str, str] | None = None,
) -> Path:
    """Return the platform default without creating or validating it."""

    env = os.environ if environ is None else environ
    family = _platform_family(platform_name)
    if family == "windows":
        local_app_data = env.get("LOCALAPPDATA")
        if not local_app_data:
            home = env.get("USERPROFILE") or str(Path.home())
            local_app_data = str(Path(home) / "AppData" / "Local")
        base = Path(local_app_data).expanduser()
        return base / "VectorMosaic" / "mathematical_research" / "projects" / "riemann_hypothesis"

    state_home = env.get("XDG_STATE_HOME")
    base = Path(state_home).expanduser() if state_home else Path.home() / ".local" / "state"
    return base / "vector_mosaic" / "mathematical_research" / "projects" / "riemann_hypothesis"


def _lexical_components(raw: str) -> tuple[str, ...]:
    # Parse both grammars.  This prevents a Windows traversal or sync marker
    # from being hidden when a fixture is inspected on Linux, and vice versa.
    components: list[str] = []
    for pure in (PureWindowsPath(raw), PurePosixPath(raw.replace("\\", "/"))):
        for part in pure.parts:
            normalized = part.rstrip("\\/")
            if normalized and normalized not in {pure.anchor, pure.drive}:
                components.append(normalized.casefold())
    return tuple(components)


def _assert_not_inside_git(resolved: Path) -> None:
    for candidate in (resolved, *resolved.parents):
        marker = candidate / ".git"
        if marker.exists():
            raise WorkspacePathError(
                f"workspace root must be outside every Git worktree: {resolved}"
            )


def _is_reparse_point(path: Path) -> bool:
    """Return true for symlinks, Windows junctions, or other reparse points."""

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


def _assert_no_reparse_chain(path: Path) -> None:
    existing: list[Path] = []
    cursor = path
    while True:
        if cursor.exists() or cursor.is_symlink():
            existing.append(cursor)
        if cursor.parent == cursor:
            break
        cursor = cursor.parent
    for candidate in reversed(existing):
        if _is_reparse_point(candidate):
            raise WorkspacePathError(
                f"workspace root cannot traverse a symlink or reparse point: {candidate}"
            )


def validate_workspace_root(root: str | os.PathLike[str]) -> Path:
    """Return one resolved local root or raise before any filesystem mutation."""

    raw = os.fspath(root)
    if not isinstance(raw, str) or not raw.strip():
        raise WorkspacePathError("workspace root must be a non-empty path")
    raw = raw.strip()
    if _URI_OR_NETWORK_PREFIX.match(raw) or raw.startswith(("\\\\", "//")):
        raise WorkspacePathError("workspace root must be a local filesystem path")

    windows = PureWindowsPath(raw)
    native = Path(raw).expanduser()
    if not native.is_absolute() and not windows.is_absolute():
        raise WorkspacePathError("workspace root must be absolute; cwd inference is forbidden")

    components = _lexical_components(raw)
    if any(part in {".", ".."} for part in components):
        raise WorkspacePathError("workspace root cannot contain traversal components")
    if "artifacts" in components:
        raise WorkspacePathError("workspace root cannot be inside artifacts/**")
    if any(part in _SYNC_COMPONENTS or part.startswith("onedrive - ") for part in components):
        raise WorkspacePathError("workspace root cannot be inside a known sync location")

    normalized_slashes = raw.replace("\\", "/").casefold()
    if normalized_slashes.startswith("/net/") or normalized_slashes.startswith("/afs/"):
        raise WorkspacePathError("workspace root cannot be inside a known network location")
    if "/gvfs/smb-share:" in normalized_slashes:
        raise WorkspacePathError("workspace root cannot be inside a mounted network share")

    lexical = native.absolute()
    _assert_no_reparse_chain(lexical)
    resolved = native.resolve(strict=False)
    _assert_not_inside_git(lexical)
    _assert_not_inside_git(resolved)
    return resolved


def normalize_logical_path(value: str | PurePosixPath) -> str:
    """Return one canonical relative POSIX path used by persisted records."""

    raw = str(value)
    if not raw or "\\" in raw or "\x00" in raw:
        raise WorkspacePathError("logical paths must be non-empty POSIX paths")
    pure = PurePosixPath(raw)
    if not pure.parts or pure.as_posix() == ".":
        raise WorkspacePathError("logical paths must identify a child of the workspace root")
    if pure.is_absolute() or raw.startswith("/"):
        raise WorkspacePathError("logical paths cannot be absolute")
    if any(part in {"", ".", ".."} for part in pure.parts):
        raise WorkspacePathError("logical paths cannot contain traversal components")
    if any(":" in part for part in pure.parts):
        raise WorkspacePathError("logical paths cannot contain drive or URI syntax")
    return pure.as_posix()


def _physical_identity(path: Path) -> str:
    identity: dict[str, object] = {"physical_path": os.path.normcase(str(path))}
    try:
        root_stat = path.stat()
    except FileNotFoundError:
        pass
    else:
        identity.update({"device": root_stat.st_dev, "inode": root_stat.st_ino})
    return hashlib.sha256(repr(sorted(identity.items())).encode("utf-8")).hexdigest()


def _utc_now() -> str:
    return (
        datetime.now(timezone.utc)
        .isoformat(timespec="microseconds")
        .replace("+00:00", "Z")
    )


def _attestation_payload(values: Mapping[str, Any]) -> bytes:
    return canonical_json_bytes(dict(values))


@dataclass(frozen=True, slots=True, init=False)
class PrincipalAttestation:
    """Process-local, OS-derived identity evidence for one supported host.

    This is deliberately distinct from caller-authored command principals.  It
    contains safe identity strings only and never retains a token handle.
    """

    platform_family: str
    host_name: str
    principal_name: str
    principal_sid: str
    session_id: int
    process_id: int
    issued_at: str
    _seal: str = field(repr=False, compare=False)

    def __init__(
        self,
        *,
        platform_family: str,
        host_name: str,
        principal_name: str,
        principal_sid: str,
        session_id: int,
        process_id: int,
        issued_at: str,
        seal: str,
        issuer: object,
    ) -> None:
        if issuer is not _ATTESTATION_ISSUER:
            raise TypeError(
                "PrincipalAttestation values are issued only by attest_current_principal"
            )
        object.__setattr__(self, "platform_family", platform_family)
        object.__setattr__(self, "host_name", host_name)
        object.__setattr__(self, "principal_name", principal_name)
        object.__setattr__(self, "principal_sid", principal_sid)
        object.__setattr__(self, "session_id", session_id)
        object.__setattr__(self, "process_id", process_id)
        object.__setattr__(self, "issued_at", issued_at)
        object.__setattr__(self, "_seal", seal)

    def to_safe_mapping(self) -> dict[str, str | int]:
        return {
            "platform_family": self.platform_family,
            "host_name": self.host_name,
            "principal_name": self.principal_name,
            "principal_sid": self.principal_sid,
            "session_id": self.session_id,
            "process_id": self.process_id,
            "issued_at": self.issued_at,
        }

    @property
    def sha256(self) -> str:
        return hashlib.sha256(_attestation_payload(self.to_safe_mapping())).hexdigest()


def verify_principal_attestation(attestation: PrincipalAttestation) -> None:
    """Verify exact module issuance and binding to the current live process."""

    if type(attestation) is not PrincipalAttestation:
        raise WorkspacePathError("principal attestation has an invalid type")
    expected = hmac.new(
        _ATTESTATION_SECRET,
        _attestation_payload(attestation.to_safe_mapping()),
        hashlib.sha256,
    ).hexdigest()
    if not hmac.compare_digest(attestation._seal, expected):
        raise WorkspacePathError("principal attestation seal is invalid")
    if (
        attestation.platform_family not in {"windows", "linux"}
        or attestation.process_id != os.getpid()
    ):
        raise WorkspacePathError("principal attestation is not bound to this process")


def stable_principal_owner_binding(attestation: PrincipalAttestation) -> str:
    """Derive one process- and session-independent writer-owner identity.

    The attestation remains fresh, issuer-sealed evidence from the current
    process.  Only the stable OS identity fields enter the durable binding;
    process ID, session ID, issuance time, and the mutable display name are
    deliberately excluded.  ``principal_sid`` carries the Windows SID or the
    Linux numeric-user identity.  The host remains part of the identity so
    writer custody cannot silently move to another host that happens to
    observe the same OS principal identifier.
    """

    verify_principal_attestation(attestation)
    material = {
        "schema_version": "mathematical_research.principal_owner_binding.v1",
        "platform_family": attestation.platform_family,
        "host_name": attestation.host_name.casefold(),
        "principal_sid": attestation.principal_sid.casefold(),
    }
    digest = hashlib.sha256(canonical_json_bytes(material)).hexdigest()
    return f"os-principal-owner:sha256:{digest}"


def _windows_principal_material() -> dict[str, str | int]:
    if os.name != "nt":
        raise WorkspacePathError("principal attestation requires Windows")
    import ctypes
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)

    host_buffer = ctypes.create_unicode_buffer(256)
    host_size = wintypes.DWORD(len(host_buffer))
    kernel32.GetComputerNameW.argtypes = (wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD))
    kernel32.GetComputerNameW.restype = wintypes.BOOL
    if not kernel32.GetComputerNameW(host_buffer, ctypes.byref(host_size)):
        raise WorkspacePathError(
            f"Windows host identity lookup failed: {ctypes.get_last_error()}"
        )

    user_buffer = ctypes.create_unicode_buffer(32768)
    user_size = wintypes.DWORD(len(user_buffer))
    advapi32.GetUserNameW.argtypes = (wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD))
    advapi32.GetUserNameW.restype = wintypes.BOOL
    if not advapi32.GetUserNameW(user_buffer, ctypes.byref(user_size)):
        raise WorkspacePathError(
            f"Windows principal-name lookup failed: {ctypes.get_last_error()}"
        )

    token = wintypes.HANDLE()
    kernel32.GetCurrentProcess.restype = wintypes.HANDLE
    advapi32.OpenProcessToken.argtypes = (
        wintypes.HANDLE,
        wintypes.DWORD,
        ctypes.POINTER(wintypes.HANDLE),
    )
    advapi32.OpenProcessToken.restype = wintypes.BOOL
    if not advapi32.OpenProcessToken(
        kernel32.GetCurrentProcess(), 0x0008, ctypes.byref(token)
    ):
        raise WorkspacePathError(
            f"Windows process-token lookup failed: {ctypes.get_last_error()}"
        )
    try:
        needed = wintypes.DWORD()
        advapi32.GetTokenInformation.argtypes = (
            wintypes.HANDLE,
            ctypes.c_int,
            ctypes.c_void_p,
            wintypes.DWORD,
            ctypes.POINTER(wintypes.DWORD),
        )
        advapi32.GetTokenInformation.restype = wintypes.BOOL
        advapi32.GetTokenInformation(token, 1, None, 0, ctypes.byref(needed))
        if needed.value == 0:
            raise WorkspacePathError(
                f"Windows token-user sizing failed: {ctypes.get_last_error()}"
            )
        token_user = ctypes.create_string_buffer(needed.value)
        if not advapi32.GetTokenInformation(
            token, 1, token_user, needed, ctypes.byref(needed)
        ):
            raise WorkspacePathError(
                f"Windows token-user lookup failed: {ctypes.get_last_error()}"
            )

        class _SidAndAttributes(ctypes.Structure):
            _fields_ = (("sid", ctypes.c_void_p), ("attributes", wintypes.DWORD))

        sid_pointer = ctypes.cast(
            token_user, ctypes.POINTER(_SidAndAttributes)
        ).contents.sid
        sid_text = ctypes.c_wchar_p()
        advapi32.ConvertSidToStringSidW.argtypes = (
            ctypes.c_void_p,
            ctypes.POINTER(ctypes.c_wchar_p),
        )
        advapi32.ConvertSidToStringSidW.restype = wintypes.BOOL
        if not advapi32.ConvertSidToStringSidW(sid_pointer, ctypes.byref(sid_text)):
            raise WorkspacePathError(
                f"Windows SID conversion failed: {ctypes.get_last_error()}"
            )
        try:
            principal_sid = str(sid_text.value)
        finally:
            kernel32.LocalFree.argtypes = (ctypes.c_void_p,)
            kernel32.LocalFree.restype = ctypes.c_void_p
            kernel32.LocalFree(ctypes.cast(sid_text, ctypes.c_void_p))
    finally:
        kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)
        kernel32.CloseHandle.restype = wintypes.BOOL
        kernel32.CloseHandle(token)

    session_id = wintypes.DWORD()
    kernel32.ProcessIdToSessionId.argtypes = (
        wintypes.DWORD,
        ctypes.POINTER(wintypes.DWORD),
    )
    kernel32.ProcessIdToSessionId.restype = wintypes.BOOL
    process_id = os.getpid()
    if not kernel32.ProcessIdToSessionId(process_id, ctypes.byref(session_id)):
        raise WorkspacePathError(
            f"Windows session identity lookup failed: {ctypes.get_last_error()}"
        )
    return {
        "host_name": host_buffer.value,
        "principal_name": user_buffer.value,
        "principal_sid": principal_sid,
        "session_id": int(session_id.value),
        "process_id": process_id,
    }


def _linux_principal_material() -> dict[str, str | int]:
    """Read the stable local user and process-session identity from Linux."""

    if _platform_family(None) != "linux" or os.name != "posix":
        raise WorkspacePathError("Linux principal attestation requires Linux")
    import pwd
    import socket

    real_uid = os.getuid()
    effective_uid = os.geteuid()
    try:
        principal_name = pwd.getpwuid(effective_uid).pw_name
    except KeyError:
        principal_name = f"uid-{effective_uid}"
    try:
        session_id = os.getsid(0)
    except OSError as exc:
        raise WorkspacePathError(
            "Linux process-session identity lookup failed"
        ) from exc
    return {
        "host_name": socket.gethostname(),
        "principal_name": principal_name,
        "principal_sid": f"linux-uid:{real_uid}:effective-uid:{effective_uid}",
        "session_id": session_id,
        "process_id": os.getpid(),
    }


def _issue_principal_attestation(
    material: Mapping[str, str | int],
    *,
    platform_family: str = "windows",
) -> PrincipalAttestation:
    if platform_family not in {"windows", "linux"}:
        raise WorkspacePathError("principal platform family is unsupported")
    required = {
        "host_name",
        "principal_name",
        "principal_sid",
        "session_id",
        "process_id",
    }
    if set(material) != required:
        raise WorkspacePathError("principal material has an invalid shape")
    for key in ("host_name", "principal_name", "principal_sid"):
        if not isinstance(material[key], str) or not str(material[key]).strip():
            raise WorkspacePathError(f"principal material {key} must be non-empty")
    for key in ("session_id", "process_id"):
        if isinstance(material[key], bool) or not isinstance(material[key], int):
            raise WorkspacePathError(f"principal material {key} must be an integer")
        if int(material[key]) < 0:
            raise WorkspacePathError(f"principal material {key} cannot be negative")
    if int(material["process_id"]) != os.getpid():
        raise WorkspacePathError("principal material is not bound to this process")
    values: dict[str, str | int] = {
        "platform_family": platform_family,
        **material,
        "issued_at": _utc_now(),
    }
    seal = hmac.new(
        _ATTESTATION_SECRET,
        _attestation_payload(values),
        hashlib.sha256,
    ).hexdigest()
    return PrincipalAttestation(
        platform_family=platform_family,
        host_name=str(values["host_name"]),
        principal_name=str(values["principal_name"]),
        principal_sid=str(values["principal_sid"]),
        session_id=int(values["session_id"]),
        process_id=int(values["process_id"]),
        issued_at=str(values["issued_at"]),
        seal=seal,
        issuer=_ATTESTATION_ISSUER,
    )


def attest_current_principal() -> PrincipalAttestation:
    """Issue OS-derived identity evidence for the current supported process."""

    family = _platform_family(None)
    if family == "windows" and os.name == "nt":
        return _issue_principal_attestation(_windows_principal_material())
    if family == "linux" and os.name == "posix":
        return _issue_principal_attestation(
            _linux_principal_material(),
            platform_family="linux",
        )
    raise WorkspacePathError(
        f"principal attestation requires a native {family} process"
    )


@dataclass(frozen=True, slots=True)
class WorkspacePaths:
    """All filesystem locations owned by one local workspace instance."""

    root: Path
    requested_root: Path = field(init=False, repr=False, compare=False)
    _root_identity: str = field(init=False, repr=False, compare=False)

    def __post_init__(self) -> None:
        supplied = Path(self.root).expanduser()
        resolved = validate_workspace_root(supplied)
        requested = supplied.absolute()
        object.__setattr__(self, "requested_root", requested)
        object.__setattr__(self, "root", resolved)
        object.__setattr__(self, "_root_identity", _physical_identity(resolved))

    @classmethod
    def from_root(cls, root: str | os.PathLike[str]) -> "WorkspacePaths":
        return cls(Path(root))

    @classmethod
    def default(
        cls,
        *,
        platform_name: str | None = None,
        environ: Mapping[str, str] | None = None,
    ) -> "WorkspacePaths":
        return cls(default_workspace_root(platform_name=platform_name, environ=environ))

    @property
    def database(self) -> Path:
        return self.root / "workspace.sqlite3"

    @property
    def cas_sha256(self) -> Path:
        return self.root / "cas" / "sha256"

    @property
    def staging(self) -> Path:
        return self.root / "staging"

    @property
    def backups(self) -> Path:
        return self.root / "backups"

    @property
    def recovery(self) -> Path:
        return self.root / "recovery"

    @property
    def root_identity(self) -> str:
        """Return an opaque host binding without persisting the absolute path."""

        return self._root_identity

    def revalidate_physical(
        self,
        *,
        require_root: bool = False,
        require_database: bool = False,
    ) -> None:
        """Recheck the physical root and every store-owned direct child."""

        current = validate_workspace_root(self.requested_root)
        if current != self.root:
            raise WorkspacePathError("workspace physical root binding changed")
        if require_root and not self.root.is_dir():
            raise WorkspacePathError("workspace root is missing or is not a directory")
        if self.root.exists() and _physical_identity(self.root) != self._root_identity:
            raise WorkspacePathError("workspace physical root identity changed")
        if self.root.exists() and (_is_reparse_point(self.root) or not self.root.is_dir()):
            raise WorkspacePathError("workspace root is not a plain local directory")
        owned_directories = (self.cas_sha256, self.staging, self.backups, self.recovery)
        for child in owned_directories:
            if child.exists() or child.is_symlink():
                if _is_reparse_point(child) or not child.is_dir():
                    raise WorkspacePathError(
                        f"workspace-owned directory is not a plain directory: {child.name}"
                    )
                if child.resolve(strict=True).parent not in {
                    self.root,
                    self.root / "cas",
                }:
                    raise WorkspacePathError("workspace-owned directory escaped its physical root")
        if self.database.exists() or self.database.is_symlink():
            if _is_reparse_point(self.database) or not self.database.is_file():
                raise WorkspacePathError("workspace database is not a plain local file")
            if self.database.resolve(strict=True).parent != self.root:
                raise WorkspacePathError("workspace database escaped its physical root")
        elif require_database:
            raise WorkspacePathError("workspace database is missing")

    def resolve_logical(self, logical_path: str | PurePosixPath) -> Path:
        """Resolve a persisted logical path and prove it remains under root."""

        normalized = normalize_logical_path(logical_path)
        candidate = (self.root / Path(*PurePosixPath(normalized).parts)).resolve(strict=False)
        try:
            candidate.relative_to(self.root)
        except ValueError as exc:
            raise WorkspacePathError("logical path escapes the workspace root") from exc
        return candidate

    def logical_from_path(self, path: str | os.PathLike[str]) -> str:
        """Convert a contained host path into canonical persisted form."""

        candidate = Path(path).expanduser().resolve(strict=False)
        try:
            relative = candidate.relative_to(self.root)
        except ValueError as exc:
            raise WorkspacePathError("path is outside the workspace root") from exc
        return normalize_logical_path(PurePosixPath(*relative.parts))

    def cas_path(self, sha256_hex: str) -> Path:
        digest = sha256_hex.casefold()
        if not re.fullmatch(r"[0-9a-f]{64}", digest):
            raise WorkspacePathError("CAS digest must be 64 lowercase hexadecimal characters")
        return self.resolve_logical(f"cas/sha256/{digest[:2]}/{digest[2:]}")
