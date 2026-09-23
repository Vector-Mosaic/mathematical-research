"""Typed Workstation contract for one targeted formal research Session.

The contract deliberately contains no repository-owned token, duration, output,
Context, attachment-count, cost, or Attempt-chain ceiling. Workstation owns
only execution effects and custody. Mathematical meaning remains with the
requesting Mathematical Research owners.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path, PurePath, PurePosixPath, PureWindowsPath
from types import MappingProxyType
from typing import Any, Mapping

from .errors import ContractError


_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]*")
_SAFE_CODE_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]*")
_DIGEST_RE = re.compile(r"[0-9a-f]{64}")
_COMMIT_RE = re.compile(r"[0-9a-f]{40,64}")
_MEDIA_TYPE_RE = re.compile(r"[A-Za-z0-9!#$&^_.+-]+/[A-Za-z0-9!#$&^_.+-]+")
_BASELINE_CAPABILITIES = frozenset({"shell", "web_search", "native_delegation"})
_WINDOWS_RESERVED_NAMES = {
    "aux",
    "con",
    "nul",
    "prn",
    *(f"com{index}" for index in range(1, 10)),
    *(f"lpt{index}" for index in range(1, 10)),
}
_SCALAR = str | int | float | bool | None


def canonical_json_bytes(value: object) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=True,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def sha256_file(path: str | Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def require_id(value: object, field_name: str) -> str:
    if not isinstance(value, str) or _ID_RE.fullmatch(value) is None:
        raise ContractError("invalid_identity", f"{field_name} is not a safe identity.")
    return value


def require_safe_code(value: object, field_name: str) -> str:
    if not isinstance(value, str) or _SAFE_CODE_RE.fullmatch(value) is None:
        raise ContractError("invalid_code", f"{field_name} is not a safe code.")
    return value


def require_digest(value: object, field_name: str) -> str:
    if not isinstance(value, str) or _DIGEST_RE.fullmatch(value) is None:
        raise ContractError("invalid_digest", f"{field_name} must be a SHA-256 digest.")
    return value


def require_commit(value: object) -> str:
    if not isinstance(value, str) or _COMMIT_RE.fullmatch(value) is None:
        raise ContractError(
            "invalid_source_commit", "source_commit must be one exact Git commit."
        )
    return value


def require_text(value: object, field_name: str) -> str:
    if not isinstance(value, str) or not value or any(
        character in value for character in "\x00\r\n"
    ):
        raise ContractError("invalid_text", f"{field_name} must be nonempty text.")
    return value


def require_capability_identifier(value: object, field_name: str) -> str:
    """Match the Mission/Host exact capability-identifier contract."""

    if (
        not isinstance(value, str)
        or not value
        or value.strip() != value
        or any(ord(character) < 32 or ord(character) == 127 for character in value)
    ):
        raise ContractError(
            "invalid_capability_identifier",
            f"{field_name} must be one exact trimmed identifier without control characters.",
        )
    return value


def require_absolute_path(value: object, field_name: str) -> str:
    if not isinstance(value, str) or not value or any(
        character in value for character in "\x00\r\n"
    ):
        raise ContractError("invalid_path", f"{field_name} must be an absolute path.")
    path = Path(value)
    if not path.is_absolute():
        raise ContractError("invalid_path", f"{field_name} must be an absolute path.")
    return str(path)


def require_lexical_absolute_path(value: object, field_name: str) -> str:
    """Validate an absolute path without resolving a possibly hostile link."""

    result = require_absolute_path(value, field_name)
    path = PurePath(result)
    if any(part in {"", ".", ".."} for part in path.parts[1:]):
        raise ContractError("invalid_path", f"{field_name} is not lexically canonical.")
    return result


def require_attachment_logical_name(value: object) -> str:
    if (
        not isinstance(value, str)
        or not value
        or "\\" in value
        or any(ord(character) < 32 for character in value)
    ):
        raise ContractError(
            "invalid_attachment_logical_name",
            "Attachment logical names must be nonempty portable relative paths.",
        )
    posix = PurePosixPath(value)
    windows = PureWindowsPath(value)
    if (
        posix.is_absolute()
        or windows.is_absolute()
        or bool(windows.drive)
        or any(part in {"", ".", ".."} for part in posix.parts)
        or any(
            part.endswith((" ", "."))
            or any(character in '<>:"|?*' for character in part)
            or part.split(".", 1)[0].casefold() in _WINDOWS_RESERVED_NAMES
            for part in posix.parts
        )
        or value != posix.as_posix()
        or value.casefold() == "bootstrap.manifest.json"
    ):
        raise ContractError(
            "invalid_attachment_logical_name",
            "Attachment logical names must be canonical relative POSIX paths.",
        )
    return value


def staged_attachment_path(root: str, logical_name: str) -> str:
    require_attachment_logical_name(logical_name)
    return str(Path(root).joinpath(*PurePosixPath(logical_name).parts))


def _validated_media_type(value: object, field_name: str) -> str:
    if not isinstance(value, str) or _MEDIA_TYPE_RE.fullmatch(value) is None:
        raise ContractError("invalid_media_type", f"{field_name} is invalid.")
    return value


def _frozen_scalar_mapping(
    value: Mapping[str, _SCALAR] | None,
    field_name: str,
) -> Mapping[str, _SCALAR]:
    if value is None:
        value = {}
    if not isinstance(value, Mapping):
        raise ContractError("invalid_resource_facts", f"{field_name} must be a mapping.")
    result: dict[str, _SCALAR] = {}
    for key, item in value.items():
        require_safe_code(key, f"{field_name}.key")
        if not isinstance(item, (str, int, float, bool, type(None))) or (
            isinstance(item, float) and (item != item or abs(item) == float("inf"))
        ):
            raise ContractError(
                "invalid_resource_facts",
                f"{field_name} values must be finite JSON scalars.",
            )
        result[key] = item
    return MappingProxyType(dict(sorted(result.items())))


class AttemptState(str, Enum):
    PREPARED = "prepared"
    LAUNCH_REQUESTED = "launch_requested"
    RUNNING = "running"
    CANCEL_REQUESTED = "cancel_requested"
    FORCE_STOP_REQUESTED = "force_stop_requested"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"
    FORCE_STOPPED = "force_stopped"
    UNKNOWN = "unknown"
    FENCED = "fenced"


TERMINAL_STATES = frozenset(
    {
        AttemptState.SUCCEEDED,
        AttemptState.FAILED,
        AttemptState.CANCELLED,
        AttemptState.FORCE_STOPPED,
        AttemptState.UNKNOWN,
        AttemptState.FENCED,
    }
)


class ProviderState(str, Enum):
    NOT_FOUND = "not_found"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"
    FORCE_STOPPED = "force_stopped"
    UNKNOWN = "unknown"


class ProviderEffectCertainty(str, Enum):
    NONE = "none"
    KNOWN = "known"
    UNKNOWN = "unknown"


class SandboxPolicy(str, Enum):
    FORMAL_WORKSPACE = "formal-workspace"


class NetworkPolicy(str, Enum):
    DENIED = "denied"
    PUBLIC = "public"


class LocalCapabilityKind(str, Enum):
    SKILL = "skill"
    PLUGIN = "plugin"


class BrowserCapabilityMode(str, Enum):
    ISOLATED_EPHEMERAL_UNAUTHENTICATED = "isolated_ephemeral_unauthenticated"


def _require_release_relative_path(value: object, field_name: str) -> str:
    if not isinstance(value, str) or not value or value.strip() != value:
        raise ContractError(
            "invalid_capability_root",
            f"{field_name} must be one canonical path relative to the immutable release.",
        )
    parts = value.split("/")
    if value.startswith("/") or "\\" in value or any(
        part in {"", ".", ".."} for part in parts
    ):
        raise ContractError(
            "invalid_capability_root",
            f"{field_name} must be one canonical path relative to the immutable release.",
        )
    return value


@dataclass(frozen=True)
class LocalCapabilityRoot:
    kind: LocalCapabilityKind
    id: str
    release_relative_path: str

    def __post_init__(self) -> None:
        try:
            object.__setattr__(self, "kind", LocalCapabilityKind(self.kind))
        except (TypeError, ValueError) as exc:
            raise ContractError(
                "invalid_capability_root", "Local capability kind must be skill or plugin."
            ) from exc
        require_capability_identifier(self.id, "local_root.id")
        object.__setattr__(
            self,
            "release_relative_path",
            _require_release_relative_path(
                self.release_relative_path, "local_root.release_relative_path"
            ),
        )

    def as_dict(self) -> dict[str, str]:
        return {
            "kind": self.kind.value,
            "id": self.id,
            "release_relative_path": self.release_relative_path,
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "LocalCapabilityRoot":
        payload = dict(value)
        if set(payload) != {"kind", "id", "release_relative_path"}:
            raise ContractError(
                "invalid_capability_root", "Local capability root has the wrong shape."
            )
        return cls(**payload)


def _validated_enabled_tools(value: object, field_name: str) -> tuple[str, ...]:
    if isinstance(value, (str, bytes)) or not isinstance(value, (tuple, list)):
        raise ContractError(
            "invalid_capability_tools", f"{field_name} must be a sequence."
        )
    tools = tuple(
        require_capability_identifier(item, f"{field_name}[{index}]")
        for index, item in enumerate(value)
    )
    if not tools or len(tools) != len(set(tools)):
        raise ContractError(
            "invalid_capability_tools",
            f"{field_name} must name at least one unique exact tool.",
        )
    return tools


@dataclass(frozen=True)
class McpServerSelection:
    server_id: str
    enabled_tools: tuple[str, ...]

    def __post_init__(self) -> None:
        require_capability_identifier(self.server_id, "mcp_server.server_id")
        object.__setattr__(
            self,
            "enabled_tools",
            _validated_enabled_tools(self.enabled_tools, "mcp_server.enabled_tools"),
        )

    def as_dict(self) -> dict[str, Any]:
        return {"server_id": self.server_id, "enabled_tools": list(self.enabled_tools)}

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "McpServerSelection":
        payload = dict(value)
        if set(payload) != {"server_id", "enabled_tools"}:
            raise ContractError(
                "invalid_capabilities", "MCP server selection has the wrong shape."
            )
        payload["enabled_tools"] = tuple(payload.get("enabled_tools", ()))
        return cls(**payload)


@dataclass(frozen=True)
class AppSelection:
    app_id: str
    enabled_tools: tuple[str, ...]

    def __post_init__(self) -> None:
        require_capability_identifier(self.app_id, "app.app_id")
        object.__setattr__(
            self,
            "enabled_tools",
            _validated_enabled_tools(self.enabled_tools, "app.enabled_tools"),
        )

    def as_dict(self) -> dict[str, Any]:
        return {"app_id": self.app_id, "enabled_tools": list(self.enabled_tools)}

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "AppSelection":
        payload = dict(value)
        if set(payload) != {"app_id", "enabled_tools"}:
            raise ContractError(
                "invalid_capabilities", "App selection has the wrong shape."
            )
        payload["enabled_tools"] = tuple(payload.get("enabled_tools", ()))
        return cls(**payload)


@dataclass(frozen=True)
class SelectedCapabilities:
    """The exact Mission-selected capability projection; empty means all extras off."""

    local_roots: tuple[LocalCapabilityRoot, ...] = ()
    mcp_servers: tuple[McpServerSelection, ...] = ()
    apps: tuple[AppSelection, ...] = ()
    browser: BrowserCapabilityMode | None = None

    def __post_init__(self) -> None:
        roots = tuple(self.local_roots)
        servers = tuple(self.mcp_servers)
        apps = tuple(self.apps)
        if any(type(item) is not LocalCapabilityRoot for item in roots):
            raise ContractError("invalid_capabilities", "local_roots are not validated.")
        if any(type(item) is not McpServerSelection for item in servers):
            raise ContractError("invalid_capabilities", "mcp_servers are not validated.")
        if any(type(item) is not AppSelection for item in apps):
            raise ContractError("invalid_capabilities", "apps are not validated.")
        if (
            len({item.id for item in roots}) != len(roots)
            or len({item.release_relative_path for item in roots}) != len(roots)
            or len({item.server_id for item in servers}) != len(servers)
            or len({item.app_id for item in apps}) != len(apps)
        ):
            raise ContractError(
                "duplicate_capability_selection",
                "Selected capability identities and local-root paths must be unique.",
            )
        if self.browser is not None:
            try:
                object.__setattr__(self, "browser", BrowserCapabilityMode(self.browser))
            except (TypeError, ValueError) as exc:
                raise ContractError(
                    "invalid_browser_capability",
                    "Browser must be isolated, ephemeral, and unauthenticated.",
                ) from exc
        object.__setattr__(self, "local_roots", roots)
        object.__setattr__(self, "mcp_servers", servers)
        object.__setattr__(self, "apps", apps)

    def as_dict(self) -> dict[str, Any]:
        return {
            "local_roots": [item.as_dict() for item in self.local_roots],
            "mcp_servers": [item.as_dict() for item in self.mcp_servers],
            "apps": [item.as_dict() for item in self.apps],
            "browser": None if self.browser is None else self.browser.value,
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "SelectedCapabilities":
        if set(value) != {"local_roots", "mcp_servers", "apps", "browser"}:
            raise ContractError(
                "invalid_capabilities", "selected_capabilities has the wrong shape."
            )
        return cls(
            local_roots=tuple(
                LocalCapabilityRoot.from_dict(item)
                for item in value.get("local_roots", ())
            ),
            mcp_servers=tuple(
                McpServerSelection.from_dict(item)
                for item in value.get("mcp_servers", ())
            ),
            apps=tuple(AppSelection.from_dict(item) for item in value.get("apps", ())),
            browser=value.get("browser"),
        )


@dataclass(frozen=True)
class ResourceRequest:
    """Actual resources requested for this Attempt, never a cumulative budget."""

    provider_class: str | None = None
    vcpu: int | None = None
    memory_mb: int | None = None
    disk_mb: int | None = None
    provider_parameters: Mapping[str, _SCALAR] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.provider_class is not None:
            require_safe_code(self.provider_class, "resource_request.provider_class")
        for name in ("vcpu", "memory_mb", "disk_mb"):
            value = getattr(self, name)
            if value is not None and (
                isinstance(value, bool) or not isinstance(value, int) or value <= 0
            ):
                raise ContractError(
                    "invalid_resource_request",
                    f"resource_request.{name} must be a positive requested fact.",
                )
        object.__setattr__(
            self,
            "provider_parameters",
            _frozen_scalar_mapping(self.provider_parameters, "provider_parameters"),
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "provider_class": self.provider_class,
            "vcpu": self.vcpu,
            "memory_mb": self.memory_mb,
            "disk_mb": self.disk_mb,
            "provider_parameters": dict(self.provider_parameters),
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "ResourceRequest":
        return cls(
            provider_class=value.get("provider_class"),
            vcpu=value.get("vcpu"),
            memory_mb=value.get("memory_mb"),
            disk_mb=value.get("disk_mb"),
            provider_parameters=value.get("provider_parameters", {}),
        )


@dataclass(frozen=True)
class ResourceObservation:
    event_sequence: int
    evidence_only: bool
    facts: Mapping[str, _SCALAR]

    def __post_init__(self) -> None:
        if isinstance(self.event_sequence, bool) or self.event_sequence <= 0:
            raise ContractError("invalid_event_sequence", "event_sequence must be positive.")
        object.__setattr__(
            self, "facts", _frozen_scalar_mapping(self.facts, "resource_observation")
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "event_sequence": self.event_sequence,
            "evidence_only": self.evidence_only,
            "facts": dict(self.facts),
        }


@dataclass(frozen=True)
class VerifiedOuterContainment:
    """Injected fact about an outer boundary Codex cannot create for itself."""

    boundary_id: str
    boundary_digest: str
    public_only_egress: bool
    private_network_denied: bool
    metadata_denied: bool
    inbound_denied: bool

    def __post_init__(self) -> None:
        require_id(self.boundary_id, "outer_containment.boundary_id")
        require_digest(self.boundary_digest, "outer_containment.boundary_digest")
        if not all(
            (
                self.public_only_egress is True,
                self.private_network_denied is True,
                self.metadata_denied is True,
                self.inbound_denied is True,
            )
        ):
            raise ContractError(
                "unverified_public_secretless_containment",
                "A public-secretless claim requires every outer network boundary fact.",
            )

    def as_dict(self) -> dict[str, Any]:
        return dict(self.__dict__)

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "VerifiedOuterContainment":
        return cls(**dict(value))


@dataclass(frozen=True)
class FormalSessionBinding:
    """Opaque binding to the Session already issued by Mathematical Research."""

    session_id: str
    session_digest: str
    mission_ref: str
    strategy_ref: str
    selected_bet_sha256: str
    context_ref: str

    def __post_init__(self) -> None:
        require_id(self.session_id, "session_id")
        require_digest(self.session_digest, "session_digest")
        require_digest(self.selected_bet_sha256, "selected_bet_sha256")
        for name in ("mission_ref", "strategy_ref", "context_ref"):
            require_text(getattr(self, name), name)

    @property
    def digest(self) -> str:
        """The MR-issued digest; WC does not derive competing Session meaning."""

        return self.session_digest

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema": "wc.formal_research_session_binding.v1",
            "session_id": self.session_id,
            "session_digest": self.session_digest,
            "mission_ref": self.mission_ref,
            "strategy_ref": self.strategy_ref,
            "selected_bet_sha256": self.selected_bet_sha256,
            "context_ref": self.context_ref,
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "FormalSessionBinding":
        payload = dict(value)
        payload.pop("schema", None)
        return cls(**payload)


@dataclass(frozen=True)
class SourceAttachment:
    logical_name: str
    source_path: str
    sha256: str
    byte_length: int
    media_type: str
    encoding: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "logical_name", require_attachment_logical_name(self.logical_name)
        )
        object.__setattr__(
            self,
            "source_path",
            require_lexical_absolute_path(self.source_path, "source_path"),
        )
        require_digest(self.sha256, "attachment.sha256")
        if (
            isinstance(self.byte_length, bool)
            or not isinstance(self.byte_length, int)
            or self.byte_length < 0
        ):
            raise ContractError("invalid_attachment_size", "byte_length must be nonnegative.")
        _validated_media_type(self.media_type, "attachment.media_type")
        if self.encoding is not None:
            require_safe_code(self.encoding, "attachment.encoding")

    def as_dict(self, *, include_source_path: bool = True) -> dict[str, Any]:
        result = {
            "logical_name": self.logical_name,
            "sha256": self.sha256,
            "byte_length": self.byte_length,
            "media_type": self.media_type,
            "encoding": self.encoding,
        }
        if include_source_path:
            result["source_path"] = self.source_path
        return result

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "SourceAttachment":
        return cls(**dict(value))


@dataclass(frozen=True)
class StagedInputAttachment:
    logical_name: str
    staged_path: str
    sha256: str
    byte_length: int
    media_type: str
    encoding: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "logical_name", require_attachment_logical_name(self.logical_name)
        )
        object.__setattr__(
            self,
            "staged_path",
            require_lexical_absolute_path(self.staged_path, "staged_path"),
        )
        require_digest(self.sha256, "staged_attachment.sha256")
        if (
            isinstance(self.byte_length, bool)
            or not isinstance(self.byte_length, int)
            or self.byte_length < 0
        ):
            raise ContractError("invalid_attachment_size", "byte_length must be nonnegative.")
        _validated_media_type(self.media_type, "staged_attachment.media_type")
        if self.encoding is not None:
            require_safe_code(self.encoding, "staged_attachment.encoding")

    def as_dict(self, *, include_path: bool = True) -> dict[str, Any]:
        result = {
            "logical_name": self.logical_name,
            "sha256": self.sha256,
            "byte_length": self.byte_length,
            "media_type": self.media_type,
            "encoding": self.encoding,
        }
        if include_path:
            result["staged_path"] = self.staged_path
        return result

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "StagedInputAttachment":
        return cls(**dict(value))


def require_sorted_source_attachments(value: object) -> tuple[SourceAttachment, ...]:
    if isinstance(value, (str, bytes)) or not isinstance(value, (tuple, list)):
        raise ContractError("invalid_attachments", "source_attachments must be a sequence.")
    result = tuple(value)
    if any(type(item) is not SourceAttachment for item in result):
        raise ContractError("invalid_attachments", "source_attachments are not validated.")
    names = [item.logical_name for item in result]
    if names != sorted(names) or len({name.casefold() for name in names}) != len(names):
        raise ContractError(
            "invalid_attachment_order",
            "Attachments must be sorted and portable-case unique.",
        )
    return result


def require_sorted_staged_attachments(value: object) -> tuple[StagedInputAttachment, ...]:
    if isinstance(value, (str, bytes)) or not isinstance(value, (tuple, list)):
        raise ContractError("invalid_attachments", "staged_attachments must be a sequence.")
    result = tuple(value)
    if any(type(item) is not StagedInputAttachment for item in result):
        raise ContractError("invalid_attachments", "staged_attachments are not validated.")
    names = [item.logical_name for item in result]
    if names != sorted(names) or len({name.casefold() for name in names}) != len(names):
        raise ContractError(
            "invalid_attachment_order",
            "Staged attachments must be sorted and portable-case unique.",
        )
    return result


@dataclass(frozen=True)
class AttemptIntent:
    """Per-Attempt operational intent under one immutable formal Session."""

    attempt_id: str
    session_id: str
    session_digest: str
    previous_attempt_id: str | None
    correction_basis: str | None
    readiness_state_digest: str
    coordination_id: str
    coordination_digest: str
    authorization_id: str
    authorization_digest: str
    source_commit: str
    source_digest: str
    provider_id: str
    provider_profile: str
    model_profile: str
    reasoning_effort: str
    resource_request: ResourceRequest
    baseline_capabilities: tuple[str, ...]
    network_policy: NetworkPolicy
    outer_containment_id: str | None
    outer_containment_digest: str | None
    source_attachments: tuple[SourceAttachment, ...]
    working_directory: str
    selected_capabilities: SelectedCapabilities = field(default_factory=SelectedCapabilities)

    def __post_init__(self) -> None:
        for name in (
            "attempt_id",
            "session_id",
            "coordination_id",
            "authorization_id",
            "provider_id",
        ):
            require_id(getattr(self, name), name)
        for name in (
            "session_digest",
            "readiness_state_digest",
            "coordination_digest",
            "authorization_digest",
            "source_digest",
        ):
            require_digest(getattr(self, name), name)
        if self.previous_attempt_id is not None:
            require_id(self.previous_attempt_id, "previous_attempt_id")
            require_text(self.correction_basis, "correction_basis")
        elif self.correction_basis is not None:
            raise ContractError(
                "invalid_retry_lineage", "A first Attempt cannot have correction_basis."
            )
        require_commit(self.source_commit)
        for name in ("provider_profile", "model_profile", "reasoning_effort"):
            require_text(getattr(self, name), name)
        if type(self.resource_request) is not ResourceRequest:
            raise ContractError("invalid_resource_request", "resource_request is not validated.")
        if type(self.selected_capabilities) is not SelectedCapabilities:
            raise ContractError(
                "invalid_capabilities", "selected_capabilities must be validated."
            )
        try:
            object.__setattr__(self, "network_policy", NetworkPolicy(self.network_policy))
        except (TypeError, ValueError) as exc:
            raise ContractError("invalid_network_policy", "network_policy is invalid.") from exc
        if self.network_policy is NetworkPolicy.DENIED and (
            self.outer_containment_id is not None
            or self.outer_containment_digest is not None
        ):
            raise ContractError(
                "invalid_outer_containment", "Denied networking cannot claim public containment."
            )
        if self.network_policy is NetworkPolicy.PUBLIC:
            require_id(self.outer_containment_id, "outer_containment_id")
            require_digest(self.outer_containment_digest, "outer_containment_digest")
        elif (
            self.outer_containment_id is None
        ) != (self.outer_containment_digest is None):
            raise ContractError(
                "invalid_outer_containment",
                "Containment identity and digest must be wholly present or absent.",
            )
        tools = tuple(self.baseline_capabilities)
        if any(not isinstance(item, str) for item in tools) or len(set(tools)) != len(tools):
            raise ContractError(
                "invalid_baseline_capabilities",
                "baseline_capabilities must be unique strings.",
            )
        for item in tools:
            require_safe_code(item, "baseline_capabilities")
        if not set(tools).issubset(_BASELINE_CAPABILITIES):
            raise ContractError(
                "invalid_baseline_capabilities",
                "baseline_capabilities may select only shell, web search, or native delegation.",
            )
        object.__setattr__(self, "baseline_capabilities", tools)
        object.__setattr__(
            self, "source_attachments", require_sorted_source_attachments(self.source_attachments)
        )
        object.__setattr__(
            self,
            "working_directory",
            require_absolute_path(self.working_directory, "working_directory"),
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema": "wc.formal_research_attempt_intent.v2",
            "attempt_id": self.attempt_id,
            "session_id": self.session_id,
            "session_digest": self.session_digest,
            "previous_attempt_id": self.previous_attempt_id,
            "correction_basis": self.correction_basis,
            "readiness_state_digest": self.readiness_state_digest,
            "coordination_id": self.coordination_id,
            "coordination_digest": self.coordination_digest,
            "authorization_id": self.authorization_id,
            "authorization_digest": self.authorization_digest,
            "source_commit": self.source_commit,
            "source_digest": self.source_digest,
            "provider_id": self.provider_id,
            "provider_profile": self.provider_profile,
            "model_profile": self.model_profile,
            "reasoning_effort": self.reasoning_effort,
            "resource_request": self.resource_request.as_dict(),
            "baseline_capabilities": list(self.baseline_capabilities),
            "network_policy": self.network_policy.value,
            "outer_containment_id": self.outer_containment_id,
            "outer_containment_digest": self.outer_containment_digest,
            "source_attachments": [item.as_dict() for item in self.source_attachments],
            "working_directory": self.working_directory,
            "selected_capabilities": self.selected_capabilities.as_dict(),
        }

    @property
    def digest(self) -> str:
        return sha256_bytes(canonical_json_bytes(self.as_dict()))

    @property
    def operational_digest(self) -> str:
        payload = self.as_dict()
        for name in ("attempt_id", "previous_attempt_id", "correction_basis"):
            payload.pop(name)
        return sha256_bytes(canonical_json_bytes(payload))

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "AttemptIntent":
        payload = dict(value)
        payload.pop("schema", None)
        payload["resource_request"] = ResourceRequest.from_dict(payload["resource_request"])
        payload["source_attachments"] = tuple(
            SourceAttachment.from_dict(item) for item in payload.get("source_attachments", [])
        )
        payload["baseline_capabilities"] = tuple(
            payload.get("baseline_capabilities", [])
        )
        payload["selected_capabilities"] = SelectedCapabilities.from_dict(
            payload.get("selected_capabilities", {})
        )
        return cls(**payload)


@dataclass(frozen=True)
class ExecutionFence:
    fence_id: str
    provider_id: str
    coordination_id: str
    coordination_digest: str
    authorization_id: str
    authorization_digest: str
    source_commit: str
    source_digest: str

    def __post_init__(self) -> None:
        for name in ("fence_id", "provider_id", "coordination_id", "authorization_id"):
            require_id(getattr(self, name), name)
        for name in ("coordination_digest", "authorization_digest", "source_digest"):
            require_digest(getattr(self, name), name)
        require_commit(self.source_commit)

    @property
    def digest(self) -> str:
        return sha256_bytes(
            canonical_json_bytes({"schema": "wc.research_attempt_fence.v3", **self.__dict__})
        )

    def matches(self, intent: AttemptIntent) -> bool:
        return all(
            (
                self.provider_id == intent.provider_id,
                self.coordination_id == intent.coordination_id,
                self.coordination_digest == intent.coordination_digest,
                self.authorization_id == intent.authorization_id,
                self.authorization_digest == intent.authorization_digest,
                self.source_commit == intent.source_commit,
                self.source_digest == intent.source_digest,
            )
        )

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "ExecutionFence":
        return cls(**dict(value))


@dataclass(frozen=True)
class OutputArtifact:
    name: str
    path: str
    sha256: str
    size_bytes: int
    media_type: str
    encoding: str | None = None
    custody_root: str | None = None
    relative_path: str | None = None

    def __post_init__(self) -> None:
        require_safe_code(self.name, "artifact.name")
        object.__setattr__(
            self, "path", require_lexical_absolute_path(self.path, "artifact.path")
        )
        require_digest(self.sha256, "artifact.sha256")
        if (
            isinstance(self.size_bytes, bool)
            or not isinstance(self.size_bytes, int)
            or self.size_bytes < 0
        ):
            raise ContractError("invalid_artifact_size", "artifact.size_bytes is invalid.")
        _validated_media_type(self.media_type, "artifact.media_type")
        if self.encoding is not None:
            require_safe_code(self.encoding, "artifact.encoding")
        if (self.custody_root is None) != (self.relative_path is None):
            raise ContractError(
                "invalid_artifact_origin",
                "Artifact custody root and relative path must be wholly present or absent.",
            )
        if self.custody_root is not None:
            if self.custody_root not in {"output", "scratch"}:
                raise ContractError(
                    "invalid_artifact_origin", "Artifact custody root is invalid."
                )
            _require_release_relative_path(self.relative_path, "artifact.relative_path")

    def as_dict(self) -> dict[str, Any]:
        return dict(self.__dict__)

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "OutputArtifact":
        return cls(**dict(value))


@dataclass(frozen=True)
class ProviderObservation:
    provider_id: str
    provider_ref: str
    state: ProviderState
    detail_code: str
    exit_code: int | None = None
    artifacts: tuple[OutputArtifact, ...] = ()
    resource_facts: Mapping[str, _SCALAR] = field(default_factory=dict)

    def __post_init__(self) -> None:
        require_id(self.provider_id, "provider_id")
        require_id(self.provider_ref, "provider_ref")
        require_safe_code(self.detail_code, "detail_code")
        try:
            object.__setattr__(self, "state", ProviderState(self.state))
        except ValueError as exc:
            raise ContractError("invalid_provider_state", "provider state is invalid.") from exc
        if self.exit_code is not None and (
            isinstance(self.exit_code, bool) or not isinstance(self.exit_code, int)
        ):
            raise ContractError("invalid_exit_code", "exit_code must be an integer.")
        artifacts = tuple(self.artifacts)
        if any(type(item) is not OutputArtifact for item in artifacts):
            raise ContractError("invalid_artifacts", "provider artifacts are not validated.")
        if len({item.name for item in artifacts}) != len(artifacts):
            raise ContractError("duplicate_artifact", "provider artifact names must be unique.")
        object.__setattr__(self, "artifacts", artifacts)
        object.__setattr__(
            self,
            "resource_facts",
            _frozen_scalar_mapping(self.resource_facts, "resource_facts"),
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "provider_id": self.provider_id,
            "provider_ref": self.provider_ref,
            "state": self.state.value,
            "detail_code": self.detail_code,
            "exit_code": self.exit_code,
            "artifacts": [item.as_dict() for item in self.artifacts],
            "resource_facts": dict(self.resource_facts),
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "ProviderObservation":
        payload = dict(value)
        payload["artifacts"] = tuple(
            OutputArtifact.from_dict(item) for item in payload.get("artifacts", [])
        )
        return cls(**payload)


@dataclass(frozen=True)
class ProviderExecutionSpec:
    attempt_id: str
    session_id: str
    session_digest: str
    fence_id: str
    provider_id: str
    provider_profile: str
    model_profile: str
    reasoning_effort: str
    sandbox_policy: SandboxPolicy
    network_policy: NetworkPolicy
    outer_containment: VerifiedOuterContainment | None
    resource_request: ResourceRequest
    baseline_capabilities: tuple[str, ...]
    source_working_directory: str
    scratch_directory: str
    output_directory: str
    input_staging_root: str
    bootstrap_manifest_path: str
    bootstrap_manifest_sha256: str
    staged_attachments: tuple[StagedInputAttachment, ...]
    protected_root: str | None
    selected_capabilities: SelectedCapabilities = field(default_factory=SelectedCapabilities)

    def __post_init__(self) -> None:
        for name in ("attempt_id", "session_id", "fence_id", "provider_id"):
            require_id(getattr(self, name), name)
        require_digest(self.session_digest, "session_digest")
        for name in ("provider_profile", "model_profile", "reasoning_effort"):
            require_text(getattr(self, name), name)
        try:
            object.__setattr__(self, "sandbox_policy", SandboxPolicy(self.sandbox_policy))
            object.__setattr__(self, "network_policy", NetworkPolicy(self.network_policy))
        except ValueError as exc:
            raise ContractError("invalid_execution_policy", "execution policy is invalid.") from exc
        if (
            self.outer_containment is not None
            and type(self.outer_containment) is not VerifiedOuterContainment
        ):
            raise ContractError(
                "invalid_outer_containment",
                "Provider execution containment must be independently verified and typed.",
            )
        if self.network_policy is NetworkPolicy.DENIED and self.outer_containment is not None:
            raise ContractError("invalid_outer_containment", "Denied networking has no public claim.")
        if self.network_policy is NetworkPolicy.PUBLIC and self.outer_containment is None:
            raise ContractError(
                "public_network_requires_verified_containment",
                "PUBLIC networking requires the exact verified containment binding.",
            )
        if type(self.resource_request) is not ResourceRequest:
            raise ContractError("invalid_resource_request", "resource_request is not validated.")
        if type(self.selected_capabilities) is not SelectedCapabilities:
            raise ContractError(
                "invalid_capabilities", "selected_capabilities must be validated."
            )
        baseline = tuple(self.baseline_capabilities)
        if (
            any(not isinstance(item, str) for item in baseline)
            or len(set(baseline)) != len(baseline)
            or not set(baseline).issubset(_BASELINE_CAPABILITIES)
        ):
            raise ContractError(
                "invalid_baseline_capabilities",
                "Provider baseline capabilities must uniquely select shell, web search, or native delegation.",
            )
        object.__setattr__(self, "baseline_capabilities", baseline)
        for name in (
            "source_working_directory",
            "scratch_directory",
            "output_directory",
            "input_staging_root",
            "bootstrap_manifest_path",
        ):
            object.__setattr__(
                self,
                name,
                require_lexical_absolute_path(getattr(self, name), name),
            )
        require_digest(self.bootstrap_manifest_sha256, "bootstrap_manifest_sha256")
        object.__setattr__(
            self,
            "staged_attachments",
            require_sorted_staged_attachments(self.staged_attachments),
        )
        if self.protected_root is not None:
            object.__setattr__(
                self,
                "protected_root",
                require_lexical_absolute_path(self.protected_root, "protected_root"),
            )
        if (
            Path(self.scratch_directory).name != self.attempt_id
            or Path(self.output_directory).name != self.attempt_id
            or Path(self.input_staging_root).name != self.attempt_id
        ):
            raise ContractError(
                "provider_execution_path_mismatch",
                "Attempt execution paths must be exact Attempt-owned directories.",
            )
        if Path(self.bootstrap_manifest_path) != Path(self.input_staging_root) / "bootstrap.manifest.json":
            raise ContractError(
                "bootstrap_manifest_path_mismatch",
                "The bootstrap manifest must be the fixed file in the staged root.",
            )

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema": "wc.formal_provider_execution.v2",
            "attempt_id": self.attempt_id,
            "session_id": self.session_id,
            "session_digest": self.session_digest,
            "fence_id": self.fence_id,
            "provider_id": self.provider_id,
            "provider_profile": self.provider_profile,
            "model_profile": self.model_profile,
            "reasoning_effort": self.reasoning_effort,
            "sandbox_policy": self.sandbox_policy.value,
            "network_policy": self.network_policy.value,
            "outer_containment": (
                None if self.outer_containment is None else self.outer_containment.as_dict()
            ),
            "resource_request": self.resource_request.as_dict(),
            "baseline_capabilities": list(self.baseline_capabilities),
            "source_working_directory": self.source_working_directory,
            "scratch_directory": self.scratch_directory,
            "output_directory": self.output_directory,
            "input_staging_root": self.input_staging_root,
            "bootstrap_manifest_path": self.bootstrap_manifest_path,
            "bootstrap_manifest_sha256": self.bootstrap_manifest_sha256,
            "staged_attachments": [item.as_dict() for item in self.staged_attachments],
            "protected_root": self.protected_root,
            "selected_capabilities": self.selected_capabilities.as_dict(),
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "ProviderExecutionSpec":
        payload = dict(value)
        payload.pop("schema", None)
        payload["sandbox_policy"] = SandboxPolicy(payload["sandbox_policy"])
        payload["network_policy"] = NetworkPolicy(payload["network_policy"])
        payload["outer_containment"] = (
            None
            if payload.get("outer_containment") is None
            else VerifiedOuterContainment.from_dict(payload["outer_containment"])
        )
        payload["resource_request"] = ResourceRequest.from_dict(
            payload["resource_request"]
        )
        payload["baseline_capabilities"] = tuple(
            payload.get("baseline_capabilities", ())
        )
        payload["staged_attachments"] = tuple(
            StagedInputAttachment.from_dict(item)
            for item in payload.get("staged_attachments", ())
        )
        payload["selected_capabilities"] = SelectedCapabilities.from_dict(
            payload.get("selected_capabilities", {})
        )
        return cls(**payload)

    @property
    def digest(self) -> str:
        return sha256_bytes(canonical_json_bytes(self.as_dict()))


@dataclass(frozen=True)
class AttemptEvent:
    sequence: int
    attempt_id: str
    operation_key: str
    event_type: str
    state: AttemptState
    recorded_at: str
    provider_ref: str | None = None
    provider_state: ProviderState | None = None
    detail_code: str | None = None
    evidence_only: bool = False
    effect_certainty: ProviderEffectCertainty = ProviderEffectCertainty.NONE
    exit_code: int | None = None
    artifacts: tuple[OutputArtifact, ...] = ()
    resource_facts: Mapping[str, _SCALAR] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if (
            isinstance(self.sequence, bool)
            or not isinstance(self.sequence, int)
            or self.sequence <= 0
        ):
            raise ContractError("invalid_event_sequence", "sequence must be positive.")
        require_id(self.attempt_id, "attempt_id")
        require_id(self.operation_key, "operation_key")
        require_safe_code(self.event_type, "event_type")
        try:
            object.__setattr__(self, "state", AttemptState(self.state))
            object.__setattr__(
                self,
                "effect_certainty",
                ProviderEffectCertainty(self.effect_certainty),
            )
            if self.provider_state is not None:
                object.__setattr__(self, "provider_state", ProviderState(self.provider_state))
        except ValueError as exc:
            raise ContractError("invalid_event", "event state is invalid.") from exc
        if self.provider_ref is not None:
            require_id(self.provider_ref, "provider_ref")
        if self.detail_code is not None:
            require_safe_code(self.detail_code, "detail_code")
        object.__setattr__(self, "artifacts", tuple(self.artifacts))
        object.__setattr__(
            self,
            "resource_facts",
            _frozen_scalar_mapping(self.resource_facts, "resource_facts"),
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "sequence": self.sequence,
            "attempt_id": self.attempt_id,
            "operation_key": self.operation_key,
            "event_type": self.event_type,
            "state": self.state.value,
            "recorded_at": self.recorded_at,
            "provider_ref": self.provider_ref,
            "provider_state": (
                None if self.provider_state is None else self.provider_state.value
            ),
            "detail_code": self.detail_code,
            "evidence_only": self.evidence_only,
            "effect_certainty": self.effect_certainty.value,
            "exit_code": self.exit_code,
            "artifacts": [item.as_dict() for item in self.artifacts],
            "resource_facts": dict(self.resource_facts),
        }

    @property
    def digest(self) -> str:
        return sha256_bytes(canonical_json_bytes(self.as_dict()))

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "AttemptEvent":
        payload = dict(value)
        payload["artifacts"] = tuple(
            OutputArtifact.from_dict(item) for item in payload.get("artifacts", [])
        )
        return cls(**payload)


@dataclass(frozen=True)
class AttemptRecord:
    session: FormalSessionBinding
    intent: AttemptIntent
    fence_id: str
    state: AttemptState
    provider_ref: str | None
    created_at: str
    last_event_at: str
    event_count: int
    input_staging_root: str
    bootstrap_manifest_path: str
    bootstrap_manifest_sha256: str
    staged_attachments: tuple[StagedInputAttachment, ...]

    @property
    def attempt_id(self) -> str:
        return self.intent.attempt_id

    @property
    def session_id(self) -> str:
        return self.session.session_id


@dataclass(frozen=True)
class ResultArtifact:
    event_sequence: int
    evidence_only: bool
    artifact: OutputArtifact

    def as_dict(self) -> dict[str, Any]:
        return {
            "event_sequence": self.event_sequence,
            "evidence_only": self.evidence_only,
            **self.artifact.as_dict(),
        }


@dataclass(frozen=True)
class AttemptResult:
    session_id: str
    session_digest: str
    attempt_id: str
    attempt_intent_digest: str
    previous_attempt_id: str | None
    source_commit: str
    source_digest: str
    provider_id: str
    provider_profile: str
    model_profile: str
    reasoning_effort: str
    fence_id: str
    attempt_state: AttemptState
    provider_effect_certainty: ProviderEffectCertainty
    provider_ref: str | None
    requested_resources: ResourceRequest
    resource_observations: tuple[ResourceObservation, ...]
    bootstrap_manifest_sha256: str
    staged_inventory_sha256: str
    artifacts: tuple[ResultArtifact, ...]
    event_history_sha256: str
    public_secretless_containment: VerifiedOuterContainment | None

    def as_dict(self, *, include_digest: bool = True) -> dict[str, Any]:
        result = {
            "schema": "wc.formal_research_attempt_result.v2",
            "session_id": self.session_id,
            "session_digest": self.session_digest,
            "attempt_id": self.attempt_id,
            "attempt_intent_digest": self.attempt_intent_digest,
            "previous_attempt_id": self.previous_attempt_id,
            "source_commit": self.source_commit,
            "source_digest": self.source_digest,
            "provider_id": self.provider_id,
            "provider_profile": self.provider_profile,
            "model_profile": self.model_profile,
            "reasoning_effort": self.reasoning_effort,
            "fence_id": self.fence_id,
            "attempt_state": self.attempt_state.value,
            "provider_effect_certainty": self.provider_effect_certainty.value,
            "provider_ref": self.provider_ref,
            "requested_resources": self.requested_resources.as_dict(),
            "resource_observations": [item.as_dict() for item in self.resource_observations],
            "bootstrap_manifest_sha256": self.bootstrap_manifest_sha256,
            "staged_inventory_sha256": self.staged_inventory_sha256,
            "artifacts": [item.as_dict() for item in self.artifacts],
            "event_history_sha256": self.event_history_sha256,
            "public_secretless_containment": (
                None
                if self.public_secretless_containment is None
                else self.public_secretless_containment.as_dict()
            ),
        }
        if include_digest:
            result["result_digest"] = self.result_digest
        return result

    @property
    def result_digest(self) -> str:
        return sha256_bytes(canonical_json_bytes(self.as_dict(include_digest=False)))


def staged_inventory_digest(items: tuple[StagedInputAttachment, ...]) -> str:
    return sha256_bytes(
        canonical_json_bytes(
            {
                "schema": "wc.formal_staged_inventory.v1",
                "files": [item.as_dict(include_path=False) for item in items],
            }
        )
    )


def event_history_digest(events: tuple[AttemptEvent, ...]) -> str:
    return sha256_bytes(
        canonical_json_bytes(
            {
                "schema": "wc.formal_attempt_event_history.v1",
                "events": [event.as_dict() for event in events],
            }
        )
    )


def artifacts_json(value: tuple[OutputArtifact, ...]) -> str:
    return canonical_json_bytes([item.as_dict() for item in value]).decode("utf-8")


def parse_artifacts_json(value: str) -> tuple[OutputArtifact, ...]:
    payload = json.loads(value)
    return tuple(OutputArtifact.from_dict(item) for item in payload)
