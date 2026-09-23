"""Provider protocol for Workstation-owned external process effects."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol, runtime_checkable

from .errors import ContractError
from .models import (
    ProviderExecutionSpec,
    ProviderObservation,
    StagedInputAttachment,
    require_digest,
    require_id,
    require_sorted_staged_attachments,
    sha256_bytes,
    staged_attachment_path,
)


@dataclass(frozen=True)
class LaunchRequest:
    attempt_id: str
    session_id: str
    fence_id: str
    execution_spec: ProviderExecutionSpec
    bootstrap_bytes: bytes = field(repr=False)
    bootstrap_sha256: str

    def __post_init__(self) -> None:
        for value, name in (
            (self.attempt_id, "attempt_id"),
            (self.session_id, "session_id"),
            (self.fence_id, "fence_id"),
        ):
            require_id(value, name)
        if type(self.execution_spec) is not ProviderExecutionSpec:
            raise ContractError(
                "invalid_provider_execution_spec", "execution_spec must be typed."
            )
        if type(self.bootstrap_bytes) is not bytes:
            raise ContractError("invalid_bootstrap", "bootstrap_bytes must be immutable bytes.")
        require_digest(self.bootstrap_sha256, "bootstrap_sha256")
        spec = self.execution_spec
        if (
            sha256_bytes(self.bootstrap_bytes) != self.bootstrap_sha256
            or self.attempt_id != spec.attempt_id
            or self.session_id != spec.session_id
            or self.fence_id != spec.fence_id
        ):
            raise ContractError(
                "bootstrap_binding_mismatch",
                "Bootstrap bytes or identities do not match the provider execution spec.",
            )


@dataclass(frozen=True)
class ProviderRequest:
    attempt_id: str
    session_id: str
    provider_ref: str | None
    execution_spec_sha256: str
    output_directory: str
    scratch_directory: str
    input_staging_root: str
    bootstrap_manifest_path: str
    bootstrap_manifest_sha256: str
    staged_attachments: tuple[StagedInputAttachment, ...]

    def __post_init__(self) -> None:
        require_id(self.attempt_id, "attempt_id")
        require_id(self.session_id, "session_id")
        if self.provider_ref is not None:
            require_id(self.provider_ref, "provider_ref")
        require_digest(self.execution_spec_sha256, "execution_spec_sha256")
        require_digest(self.bootstrap_manifest_sha256, "bootstrap_manifest_sha256")
        staged = require_sorted_staged_attachments(self.staged_attachments)
        object.__setattr__(self, "staged_attachments", staged)
        for name in (
            "output_directory",
            "scratch_directory",
            "input_staging_root",
            "bootstrap_manifest_path",
        ):
            value = Path(getattr(self, name))
            if not value.is_absolute():
                raise ContractError("invalid_provider_path", f"{name} must be absolute.")
        if (
            Path(self.output_directory).name != self.attempt_id
            or Path(self.scratch_directory).name != self.attempt_id
            or Path(self.input_staging_root).name != self.attempt_id
            or Path(self.bootstrap_manifest_path)
            != Path(self.input_staging_root) / "bootstrap.manifest.json"
            or any(
                item.staged_path
                != staged_attachment_path(self.input_staging_root, item.logical_name)
                for item in staged
            )
        ):
            raise ContractError(
                "provider_execution_path_mismatch",
                "Provider request paths do not match the exact Attempt layout.",
            )


@runtime_checkable
class AttemptProvider(Protocol):
    @property
    def provider_id(self) -> str:
        ...

    def launch(self, request: LaunchRequest) -> ProviderObservation:
        ...

    def request_cancel(self, request: ProviderRequest) -> ProviderObservation:
        ...

    def force_stop(self, request: ProviderRequest) -> ProviderObservation:
        ...

    def reconcile(self, request: ProviderRequest) -> ProviderObservation:
        ...
