"""Durable per-Attempt worker identity, control, and complete file custody."""

from __future__ import annotations

import codecs
import ctypes
import hashlib
import json
import mimetypes
import os
import stat
import uuid
from pathlib import Path
from typing import Mapping

from .errors import ProviderEffectUnknownError
from .models import (
    OutputArtifact,
    canonical_json_bytes,
    sha256_bytes,
)


def read_json_object(path: Path) -> dict[str, object]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ProviderEffectUnknownError(
            "codex_state_unreadable", "Codex provider state is unreadable."
        ) from exc
    if not isinstance(payload, dict):
        raise ProviderEffectUnknownError(
            "codex_state_unreadable", "Codex provider state must be an object."
        )
    return payload


def write_json_atomic(path: Path, payload: Mapping[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    with temporary.open("wb") as handle:
        handle.write(canonical_json_bytes(payload))
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)


def write_json_once(path: Path, payload: Mapping[str, object]) -> None:
    encoded = canonical_json_bytes(payload)
    if path.exists():
        try:
            existing = path.read_bytes()
        except OSError as exc:
            raise ProviderEffectUnknownError(
                "codex_state_unreadable", "Codex provider state is unreadable."
            ) from exc
        if existing != encoded:
            raise ProviderEffectUnknownError(
                "codex_state_binding_conflict",
                "Immutable Codex provider state was reused with different content.",
            )
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp")
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    descriptor = os.open(temporary, flags, 0o600)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            descriptor = -1
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
        try:
            os.link(temporary, path)
        except FileExistsError:
            if path.read_bytes() != encoded:
                raise ProviderEffectUnknownError(
                    "codex_state_binding_conflict",
                    "Immutable Codex provider state was reused with different content.",
                )
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def _is_link_or_reparse(details: os.stat_result) -> bool:
    reparse = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    return stat.S_ISLNK(details.st_mode) or bool(
        getattr(details, "st_file_attributes", 0) & reparse
    )


def require_no_link_components(path: Path, role: str) -> None:
    current = Path(path.anchor)
    for part in path.parts[1:]:
        current /= part
        if not current.exists() and not current.is_symlink():
            continue
        try:
            details = current.lstat()
        except OSError as exc:
            raise ProviderEffectUnknownError(
                "codex_output_unobservable", f"{role} could not be inspected."
            ) from exc
        if _is_link_or_reparse(details):
            raise ProviderEffectUnknownError(
                "codex_output_unsafe", f"{role} contains a link or reparse point."
            )


def _stable_file(
    path: Path,
    role: str,
    *,
    detect_utf8: bool = False,
) -> tuple[os.stat_result, str, str | None]:
    """Hash one stable file and opportunistically validate UTF-8 in that stream."""

    require_no_link_components(path, role)
    try:
        before = path.lstat()
    except OSError as exc:
        raise ProviderEffectUnknownError(
            "codex_output_unobservable", f"{role} could not be inspected."
        ) from exc
    if (
        _is_link_or_reparse(before)
        or not stat.S_ISREG(before.st_mode)
        or before.st_nlink != 1
    ):
        raise ProviderEffectUnknownError(
            "codex_output_unsafe", f"{role} is not a single-link regular file."
        )
    digest = hashlib.sha256()
    decoder = codecs.getincrementaldecoder("utf-8")("strict") if detect_utf8 else None
    try:
        with path.open("rb") as stream:
            while True:
                chunk = stream.read(1024 * 1024)
                if not chunk:
                    break
                digest.update(chunk)
                if decoder is not None:
                    try:
                        decoder.decode(chunk)
                    except UnicodeDecodeError:
                        decoder = None
            if decoder is not None:
                try:
                    decoder.decode(b"", final=True)
                except UnicodeDecodeError:
                    decoder = None
    except OSError as exc:
        raise ProviderEffectUnknownError(
            "codex_output_unobservable", f"{role} could not be hashed."
        ) from exc
    try:
        after = path.lstat()
    except OSError as exc:
        raise ProviderEffectUnknownError(
            "codex_output_changed", f"{role} changed during hashing."
        ) from exc
    identity_before = (
        before.st_dev,
        before.st_ino,
        before.st_size,
        before.st_mtime_ns,
        before.st_nlink,
    )
    identity_after = (
        after.st_dev,
        after.st_ino,
        after.st_size,
        after.st_mtime_ns,
        after.st_nlink,
    )
    if identity_before != identity_after or _is_link_or_reparse(after):
        raise ProviderEffectUnknownError(
            "codex_output_changed", f"{role} changed during hashing."
        )
    return after, digest.hexdigest(), "utf-8" if decoder is not None else None


_EXPLICIT_TEXT_MEDIA_TYPES = {
    ".csv": "text/csv",
    ".jsonl": "application/x-ndjson",
    ".log": "text/plain",
    ".ndjson": "application/x-ndjson",
}
_UTF8_APPLICATION_MEDIA_TYPES = {
    "application/javascript",
    "application/json",
    "application/sql",
    "application/x-ndjson",
    "application/xml",
}


def _artifact_media_type(relative_path: str) -> str:
    explicit = _EXPLICIT_TEXT_MEDIA_TYPES.get(Path(relative_path).suffix.lower())
    if explicit is not None:
        return explicit
    inferred = mimetypes.guess_type(relative_path, strict=False)[0]
    return inferred or "application/octet-stream"


def _may_be_utf8(media_type: str) -> bool:
    return media_type.startswith("text/") or media_type in _UTF8_APPLICATION_MEDIA_TYPES


def collect_complete_artifacts(
    output_root: Path, scratch_root: Path
) -> tuple[OutputArtifact, ...]:
    """Inventory declared output and operational scratch without following links."""

    artifacts: list[OutputArtifact] = []
    file_identities: set[tuple[int, int]] = set()
    for custody_root, root in (("output", output_root), ("scratch", scratch_root)):
        require_no_link_components(root, f"codex_{custody_root}_root")
        try:
            root_details = root.lstat()
        except OSError as exc:
            raise ProviderEffectUnknownError(
                "codex_output_unobservable",
                f"Codex {custody_root} root could not be inspected.",
            ) from exc
        if _is_link_or_reparse(root_details) or not stat.S_ISDIR(root_details.st_mode):
            raise ProviderEffectUnknownError(
                "codex_output_unsafe", f"Codex {custody_root} root is unsafe."
            )
        pending = [root]
        files: list[Path] = []
        while pending:
            directory = pending.pop()
            try:
                entries = sorted(os.scandir(directory), key=lambda item: item.name)
            except OSError as exc:
                raise ProviderEffectUnknownError(
                    "codex_output_unobservable", "Codex output inventory failed."
                ) from exc
            for entry in entries:
                path = Path(entry.path)
                try:
                    details = entry.stat(follow_symlinks=False)
                except OSError as exc:
                    raise ProviderEffectUnknownError(
                        "codex_output_unobservable", "Codex output inventory changed."
                    ) from exc
                if _is_link_or_reparse(details):
                    raise ProviderEffectUnknownError(
                        "codex_output_unsafe",
                        "Codex output contains a link or reparse point.",
                    )
                if stat.S_ISDIR(details.st_mode):
                    pending.append(path)
                elif stat.S_ISREG(details.st_mode):
                    files.append(path)
                else:
                    raise ProviderEffectUnknownError(
                        "codex_output_unsafe",
                        "Codex output contains a non-regular filesystem object.",
                    )
        for path in sorted(files, key=lambda item: item.relative_to(root).as_posix()):
            relative_path = path.relative_to(root).as_posix()
            media_type = _artifact_media_type(relative_path)
            details, digest, encoding = _stable_file(
                path,
                "codex_output_file",
                detect_utf8=_may_be_utf8(media_type),
            )
            identity = (details.st_dev, details.st_ino)
            if identity in file_identities:
                raise ProviderEffectUnknownError(
                    "codex_output_hardlink",
                    "Codex output inventory contains aliased file identities.",
                )
            file_identities.add(identity)
            artifacts.append(
                OutputArtifact(
                    name="artifact-"
                    + sha256_bytes(
                        canonical_json_bytes(
                            {
                                "custody_root": custody_root,
                                "relative_path": relative_path,
                            }
                        )
                    ),
                    path=str(path),
                    sha256=digest,
                    size_bytes=details.st_size,
                    media_type=media_type,
                    encoding=encoding,
                    custody_root=custody_root,
                    relative_path=relative_path,
                )
            )
    return tuple(artifacts)


def process_identity(pid: int) -> dict[str, object] | None:
    """Return a platform process-birth identity, or None when the PID is absent."""

    if isinstance(pid, bool) or not isinstance(pid, int) or pid <= 0:
        return None
    if os.name == "nt":
        process_query_limited_information = 0x1000
        kernel32 = ctypes.windll.kernel32
        kernel32.OpenProcess.restype = ctypes.c_void_p
        kernel32.OpenProcess.argtypes = [ctypes.c_uint32, ctypes.c_int, ctypes.c_uint32]
        kernel32.GetProcessTimes.argtypes = [
            ctypes.c_void_p,
            ctypes.c_void_p,
            ctypes.c_void_p,
            ctypes.c_void_p,
            ctypes.c_void_p,
        ]
        kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
        handle = kernel32.OpenProcess(
            process_query_limited_information, False, pid
        )
        if not handle:
            return None
        try:
            creation = ctypes.c_ulonglong()
            exit_time = ctypes.c_ulonglong()
            kernel = ctypes.c_ulonglong()
            user = ctypes.c_ulonglong()
            if not kernel32.GetProcessTimes(
                handle,
                ctypes.byref(creation),
                ctypes.byref(exit_time),
                ctypes.byref(kernel),
                ctypes.byref(user),
            ):
                return None
            return {
                "platform": "windows",
                "pid": pid,
                "creation_filetime": int(creation.value),
            }
        finally:
            kernel32.CloseHandle(handle)
    stat_path = Path("/proc") / str(pid) / "stat"
    try:
        raw = stat_path.read_text(encoding="ascii")
        close = raw.rfind(")")
        fields = raw[close + 2 :].split()
        start_ticks = fields[19]
        process_group = int(fields[2])
        boot_id = Path("/proc/sys/kernel/random/boot_id").read_text(
            encoding="ascii"
        ).strip()
    except (OSError, ValueError, IndexError):
        return None
    return {
        "platform": "linux",
        "pid": pid,
        "boot_id": boot_id,
        "start_ticks": start_ticks,
        "process_group": process_group,
    }


def identity_matches(expected: object) -> bool:
    if not isinstance(expected, dict) or not isinstance(expected.get("pid"), int):
        return False
    return process_identity(int(expected["pid"])) == expected
