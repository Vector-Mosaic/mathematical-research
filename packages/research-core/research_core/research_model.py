"""Immutable domain records for the read-only research reference model."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from types import MappingProxyType
from typing import Any, Generic, Mapping, TypeVar

from .validator import ValidationResult


class SourceClass(str, Enum):
    """Authority class declared by every reference-model load."""

    LIVE_CANONICAL = "live_canonical"
    HISTORICAL_FIXTURE = "historical_fixture"
    SYNTHETIC_FIXTURE = "synthetic_fixture"


def deep_freeze(value: Any) -> Any:
    """Recursively remove mutable aliases from JSON-like data."""

    if isinstance(value, Mapping):
        return MappingProxyType(
            {str(key): deep_freeze(item) for key, item in value.items()}
        )
    if isinstance(value, (list, tuple)):
        return tuple(deep_freeze(item) for item in value)
    if isinstance(value, (set, frozenset)):
        return frozenset(deep_freeze(item) for item in value)
    return value


def deep_thaw(value: Any) -> Any:
    """Copy immutable JSON-like data into ordinary serialization containers."""

    if isinstance(value, Mapping):
        return {str(key): deep_thaw(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [deep_thaw(item) for item in value]
    if isinstance(value, (set, frozenset)):
        return sorted((deep_thaw(item) for item in value), key=repr)
    return value


@dataclass(frozen=True, slots=True)
class AuthorityVector:
    """Minimal versioned binding to one exact canonical Research State release."""

    source_class: SourceClass
    canonical_state_path: str
    canonical_state_sha256: str
    source_commit: str | None = None
    binding_version: int = 2

    def __post_init__(self) -> None:
        if type(self.binding_version) is not int or self.binding_version != 2:
            raise ValueError("canonical authority binding_version must be 2")
        if type(self.source_class) is not SourceClass:
            raise TypeError("canonical authority source_class must be exact")
        if type(self.canonical_state_path) is not str or not self.canonical_state_path:
            raise ValueError("canonical authority path must be non-empty text")
        if re.fullmatch(r"[0-9a-f]{64}", self.canonical_state_sha256) is None:
            raise ValueError(
                "canonical authority state digest must be lowercase sha256"
            )
        if self.source_commit is not None and (
            type(self.source_commit) is not str
            or re.fullmatch(r"[0-9a-f]{40}", self.source_commit) is None
        ):
            raise ValueError("canonical authority source_commit must be a full Git SHA")

    def to_mapping(self) -> dict[str, Any]:
        """Return the exact five-key v2 persistence representation."""

        return {
            "binding_version": self.binding_version,
            "source_class": self.source_class.value,
            "canonical_state_path": self.canonical_state_path,
            "canonical_state_sha256": self.canonical_state_sha256,
            "source_commit": self.source_commit,
        }


@dataclass(frozen=True, slots=True)
class OperationFailure:
    code: str
    location: str
    message: str

    def to_mapping(self) -> dict[str, str]:
        return {
            "code": self.code,
            "location": self.location,
            "message": self.message,
        }


T = TypeVar("T")


@dataclass(frozen=True, slots=True)
class OperationResult(Generic[T]):
    """Proof-neutral result envelope shared by reference-model operations."""

    value: T | None
    findings: tuple[Any, ...] = ()
    authority_vector: AuthorityVector | None = None
    metrics: Mapping[str, Any] = field(default_factory=lambda: MappingProxyType({}))
    failure: OperationFailure | None = None
    mathematical_effect: str = "none"

    def __post_init__(self) -> None:
        object.__setattr__(self, "findings", tuple(self.findings))
        object.__setattr__(self, "metrics", deep_freeze(dict(self.metrics)))
        if self.mathematical_effect != "none":
            raise ValueError("reference-model operations are mathematically inert")
        if self.value is not None and self.failure is not None:
            raise ValueError("an operation result cannot contain value and failure")

    @property
    def ok(self) -> bool:
        return (
            self.value is not None
            and self.failure is None
            and not any(
                getattr(item, "severity", None) == "ERROR" for item in self.findings
            )
        )


@dataclass(frozen=True, slots=True)
class CanonicalStateSnapshot:
    """One exact, validated, deeply immutable Research State capture."""

    authority_vector: AuthorityVector
    authorized_path: Path
    raw_sha256: str
    schema_version: int
    state: Mapping[str, Any]
    validation: ValidationResult
    normalized_digest: str
    repo_root: Path | None = None

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "authorized_path",
            Path(self.authorized_path).resolve(),
        )
        if self.repo_root is not None:
            object.__setattr__(self, "repo_root", Path(self.repo_root).resolve())
        object.__setattr__(self, "state", deep_freeze(self.state))
        if type(self.authority_vector) is not AuthorityVector:
            raise TypeError("canonical snapshot requires an exact AuthorityVector")
        if type(self.schema_version) is not int or isinstance(
            self.schema_version, bool
        ):
            raise TypeError("canonical snapshot schema_version must be an integer")
        if type(self.validation) is not ValidationResult:
            raise TypeError("canonical snapshot validation must be exact")
        if re.fullmatch(r"[0-9a-f]{64}", self.raw_sha256) is None:
            raise ValueError("canonical snapshot raw digest must be lowercase sha256")
        if re.fullmatch(r"[0-9a-f]{64}", self.normalized_digest) is None:
            raise ValueError(
                "canonical snapshot normalized digest must be lowercase sha256"
            )
        if self.raw_sha256 != self.authority_vector.canonical_state_sha256:
            raise ValueError(
                "canonical snapshot digest differs from its authority binding"
            )
