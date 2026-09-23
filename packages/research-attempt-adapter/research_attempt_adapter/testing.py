"""Test-only deterministic doubles; never use to launch or verify real research."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable

from .errors import (
    ProviderEffectUnknownError,
    ProviderRejectedError,
    SourceVerificationError,
)
from .models import AttemptIntent, ProviderObservation, ProviderState
from .provider import LaunchRequest, ProviderRequest
from .source_verifier import SourceVerification


@dataclass
class FakeProviderBackend:
    observations: dict[str, ProviderObservation] = field(default_factory=dict)
    execution_spec_digests: dict[str, str] = field(default_factory=dict)
    launch_calls: int = 0
    cancel_calls: int = 0
    force_stop_calls: int = 0
    reconcile_calls: int = 0
    next_launch_error: str | None = None
    next_cancel_error: str | None = None
    next_force_stop_error: str | None = None
    on_launch: Callable[[LaunchRequest], None] | None = None
    on_cancel: Callable[[ProviderRequest], None] | None = None
    on_force_stop: Callable[[ProviderRequest], None] | None = None


class FakeAttemptProvider:
    def __init__(
        self,
        backend: FakeProviderBackend | None = None,
        *,
        provider_id: str = "fake_provider",
    ) -> None:
        self.backend = backend or FakeProviderBackend()
        self._provider_id = provider_id

    @property
    def provider_id(self) -> str:
        return self._provider_id

    def _ref(self, attempt_id: str) -> str:
        return f"fake:{attempt_id}"

    def _bind(self, request: LaunchRequest) -> None:
        digest = request.execution_spec.digest
        previous = self.backend.execution_spec_digests.get(request.attempt_id)
        if previous is not None and previous != digest:
            raise ProviderRejectedError(
                "fake_attempt_binding_changed",
                "The fake provider Attempt binding changed after first launch.",
            )
        self.backend.execution_spec_digests[request.attempt_id] = digest

    def _validate_request(self, request: ProviderRequest) -> None:
        expected = self.backend.execution_spec_digests.get(request.attempt_id)
        if expected is not None and expected != request.execution_spec_sha256:
            raise ProviderRejectedError(
                "fake_attempt_binding_changed",
                "The fake provider request does not match the launch binding.",
            )

    def launch(self, request: LaunchRequest) -> ProviderObservation:
        self.backend.launch_calls += 1
        self._bind(request)
        if self.backend.on_launch is not None:
            self.backend.on_launch(request)
        error = self.backend.next_launch_error
        self.backend.next_launch_error = None
        if error == "unknown":
            raise ProviderEffectUnknownError(
                "fake_launch_unknown", "The fake launch effect is deliberately unknown."
            )
        if error == "rejected":
            raise ProviderRejectedError(
                "fake_launch_rejected", "The fake launch was rejected before effect."
            )
        observation = self.backend.observations.get(request.attempt_id)
        if observation is None:
            observation = ProviderObservation(
                provider_id=self.provider_id,
                provider_ref=self._ref(request.attempt_id),
                state=ProviderState.RUNNING,
                detail_code="fake_running",
            )
            self.backend.observations[request.attempt_id] = observation
        return observation

    def request_cancel(self, request: ProviderRequest) -> ProviderObservation:
        self.backend.cancel_calls += 1
        self._validate_request(request)
        if self.backend.on_cancel is not None:
            self.backend.on_cancel(request)
        error = self.backend.next_cancel_error
        self.backend.next_cancel_error = None
        if error == "unknown":
            raise ProviderEffectUnknownError(
                "fake_cancel_unknown", "The fake cancel effect is deliberately unknown."
            )
        if error == "rejected":
            raise ProviderRejectedError(
                "fake_cancel_rejected", "The fake cancel was rejected before effect."
            )
        existing = self.backend.observations.get(request.attempt_id)
        if existing is None:
            return ProviderObservation(
                provider_id=self.provider_id,
                provider_ref=request.provider_ref or self._ref(request.attempt_id),
                state=ProviderState.NOT_FOUND,
                detail_code="fake_not_found",
            )
        if existing.state is not ProviderState.RUNNING:
            return existing
        observation = ProviderObservation(
            provider_id=self.provider_id,
            provider_ref=existing.provider_ref,
            state=ProviderState.RUNNING,
            detail_code="fake_cancel_requested",
            resource_facts=existing.resource_facts,
        )
        self.backend.observations[request.attempt_id] = observation
        return observation

    def force_stop(self, request: ProviderRequest) -> ProviderObservation:
        self.backend.force_stop_calls += 1
        self._validate_request(request)
        if self.backend.on_force_stop is not None:
            self.backend.on_force_stop(request)
        error = self.backend.next_force_stop_error
        self.backend.next_force_stop_error = None
        if error == "unknown":
            raise ProviderEffectUnknownError(
                "fake_force_stop_unknown", "The fake force-stop effect is unknown."
            )
        if error == "rejected":
            raise ProviderRejectedError(
                "fake_force_stop_rejected", "The fake force-stop was rejected."
            )
        existing = self.backend.observations.get(request.attempt_id)
        if existing is None:
            return ProviderObservation(
                provider_id=self.provider_id,
                provider_ref=request.provider_ref or self._ref(request.attempt_id),
                state=ProviderState.NOT_FOUND,
                detail_code="fake_not_found",
            )
        if existing.state is not ProviderState.RUNNING:
            return existing
        observation = ProviderObservation(
            provider_id=self.provider_id,
            provider_ref=existing.provider_ref,
            state=ProviderState.RUNNING,
            detail_code="fake_force_stop_requested",
            resource_facts=existing.resource_facts,
        )
        self.backend.observations[request.attempt_id] = observation
        return observation

    def reconcile(self, request: ProviderRequest) -> ProviderObservation:
        self.backend.reconcile_calls += 1
        self._validate_request(request)
        return self.backend.observations.get(
            request.attempt_id,
            ProviderObservation(
                provider_id=self.provider_id,
                provider_ref=request.provider_ref or self._ref(request.attempt_id),
                state=ProviderState.NOT_FOUND,
                detail_code="fake_not_found",
            ),
        )

    def set_observation(
        self, attempt_id: str, observation: ProviderObservation
    ) -> None:
        self.backend.observations[attempt_id] = observation


class FakeSourceVerifier:
    """Injected verifier for deterministic tests without provider-side Git effects."""

    def __init__(self, *, reject: bool = False) -> None:
        self.reject = reject
        self.calls = 0

    def verify(self, intent: AttemptIntent) -> SourceVerification:
        self.calls += 1
        if self.reject:
            raise SourceVerificationError(
                "source_verification_rejected",
                "The injected source verifier rejected the launch worktree.",
            )
        return SourceVerification(
            verifier_id="fake_source_verifier",
            source_commit=intent.source_commit,
            source_digest=intent.source_digest,
        )

