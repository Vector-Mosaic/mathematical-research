"""Workstation owner for formal Session staging, effects, and artifact custody."""

from __future__ import annotations

import os
import shutil
import stat
from pathlib import Path
from typing import Callable, Mapping

from .errors import (
    AttemptBlockedError,
    ContractError,
    InjectedCrash,
    ProviderEffectUnknownError,
    ProviderRejectedError,
)
from .journal import AttemptJournal
from .models import (
    AttemptIntent,
    AttemptRecord,
    AttemptResult,
    AttemptState,
    ExecutionFence,
    FormalSessionBinding,
    NetworkPolicy,
    OutputArtifact,
    ProviderEffectCertainty,
    ProviderExecutionSpec,
    ProviderObservation,
    ProviderState,
    ResourceObservation,
    ResultArtifact,
    SandboxPolicy,
    SourceAttachment,
    StagedInputAttachment,
    VerifiedOuterContainment,
    canonical_json_bytes,
    event_history_digest,
    sha256_bytes,
    sha256_file,
    staged_attachment_path,
    staged_inventory_digest,
)
from .provider import AttemptProvider, LaunchRequest, ProviderRequest
from .source_verifier import SourceVerifier


_ACTIVE_STATES = frozenset(
    {
        AttemptState.LAUNCH_REQUESTED,
        AttemptState.RUNNING,
        AttemptState.CANCEL_REQUESTED,
        AttemptState.FORCE_STOP_REQUESTED,
    }
)
_RETRYABLE_STATES = frozenset(
    {
        AttemptState.FAILED,
        AttemptState.CANCELLED,
        AttemptState.FORCE_STOPPED,
    }
)


class ResearchAttemptAdapter:
    """One mechanism for formal provider execution; no semantic scheduling."""

    def __init__(
        self,
        *,
        journal: AttemptJournal,
        providers: Mapping[str, AttemptProvider],
        source_verifier: SourceVerifier,
        input_stage_store_root: str | Path,
        scratch_store_root: str | Path,
        provider_output_root: str | Path,
        artifact_store_root: str | Path,
        protected_root: str | Path | None = None,
        source_attachment_roots: tuple[str | Path, ...] = (),
        outer_containment: VerifiedOuterContainment | None = None,
        fault_injector: Callable[[str, AttemptRecord], None] | None = None,
    ) -> None:
        self.journal = journal
        self.providers = dict(providers)
        self.source_verifier = source_verifier
        self.input_stage_store_root = Path(input_stage_store_root).absolute()
        self.scratch_store_root = Path(scratch_store_root).absolute()
        self.provider_output_root = Path(provider_output_root).absolute()
        self.artifact_store_root = Path(artifact_store_root).absolute()
        self.protected_root = (
            None if protected_root is None else self._normalize_exact_root(protected_root)
        )
        if (
            outer_containment is not None
            and type(outer_containment) is not VerifiedOuterContainment
        ):
            raise ContractError(
                "invalid_outer_containment",
                "Adapter outer containment must be an independently verified fact.",
            )
        self.outer_containment = outer_containment
        self.fault_injector = fault_injector
        roots = (
            self.input_stage_store_root,
            self.scratch_store_root,
            self.provider_output_root,
            self.artifact_store_root,
        )
        for root in roots:
            self._ensure_owned_directory(root)
        for index, first in enumerate(roots):
            for second in roots[index + 1 :]:
                if self._paths_overlap(first, second):
                    raise ContractError(
                        "execution_roots_overlap",
                        "Input, scratch, provider-output, and seal roots must be disjoint.",
                    )
        if self.protected_root is not None:
            self._require_directory_without_links(self.protected_root, "protected_root")
        if self.protected_root is not None and any(
            self._paths_overlap(self.protected_root, root) for root in roots
        ):
            raise ContractError(
                "protected_root_overlap", "The protected root must be outside Attempt roots."
            )
        normalized_source_roots: list[Path] = []
        for configured_root in source_attachment_roots:
            source_root = self._normalize_exact_root(configured_root)
            self._require_directory_without_links(source_root, "source_attachment_root")
            if any(self._paths_overlap(source_root, root) for root in roots):
                raise ContractError(
                    "source_attachment_root_overlap",
                    "Authorized source roots must be outside Attempt-owned stores.",
                )
            if self.protected_root is not None and self._paths_overlap(
                source_root, self.protected_root
            ):
                raise ContractError(
                    "source_attachment_root_protected_overlap",
                    "Authorized source roots must be outside the protected root.",
                )
            normalized_source_roots.append(source_root)
        self.source_attachment_roots = tuple(normalized_source_roots)
        for provider_id, provider in self.providers.items():
            if provider.provider_id != provider_id:
                raise ContractError(
                    "provider_identity_mismatch", "Provider registry identity is inconsistent."
                )

    def _inject(self, point: str, record: AttemptRecord) -> None:
        if self.fault_injector is not None:
            self.fault_injector(point, record)

    def _provider(self, provider_id: str) -> AttemptProvider:
        provider = self.providers.get(provider_id)
        if provider is None:
            raise ContractError("provider_not_registered", "Provider is not registered.")
        return provider

    def install_fence(self, fence: ExecutionFence) -> ExecutionFence:
        return self.journal.install_fence(fence)

    def prepare(
        self,
        session: FormalSessionBinding,
        intent: AttemptIntent,
        *,
        fence_id: str,
    ) -> AttemptRecord:
        if intent.previous_attempt_id is not None:
            raise ContractError(
                "retry_requires_retry_api", "Retry Attempts must use retry()."
            )
        return self._prepare(session, intent, fence_id=fence_id)

    def retry(
        self,
        previous_attempt_id: str,
        intent: AttemptIntent,
        *,
        fence_id: str,
    ) -> AttemptRecord:
        previous = self.journal.get_attempt(previous_attempt_id)
        if previous.state not in _RETRYABLE_STATES:
            code = (
                "retry_blocked_unknown"
                if previous.state is AttemptState.UNKNOWN
                else "retry_requires_known_failure_or_cancel"
            )
            raise AttemptBlockedError(
                code,
                "Retry requires a known terminal failure/cancel and never follows UNKNOWN.",
            )
        if (
            intent.previous_attempt_id != previous_attempt_id
            or intent.session_id != previous.session_id
            or intent.session_digest != previous.session.digest
        ):
            raise ContractError(
                "retry_lineage_mismatch", "Retry must remain under the exact immutable Session."
            )
        prior_binding = self._retry_material_payload(previous.intent)
        next_binding = self._retry_material_payload(intent)
        if (
            prior_binding == next_binding
            and intent.readiness_state_digest == previous.intent.readiness_state_digest
        ):
            raise AttemptBlockedError(
                "retry_without_material_correction",
                "An unchanged operational binding and readiness state cannot be retried.",
            )
        return self._prepare(previous.session, intent, fence_id=fence_id)

    @staticmethod
    def _retry_material_payload(intent: AttemptIntent) -> bytes:
        payload = intent.as_dict()
        for name in (
            "attempt_id",
            "previous_attempt_id",
            "correction_basis",
            "readiness_state_digest",
        ):
            payload.pop(name)
        return canonical_json_bytes(payload)

    def _prepare(
        self,
        session: FormalSessionBinding,
        intent: AttemptIntent,
        *,
        fence_id: str,
    ) -> AttemptRecord:
        if session.session_id != intent.session_id or session.digest != intent.session_digest:
            raise ContractError(
                "session_attempt_mismatch", "Attempt does not bind the exact formal Session."
            )
        fence = self.journal.get_fence(fence_id)
        if not self.journal.is_fence_active(fence_id) or not fence.matches(intent):
            raise AttemptBlockedError(
                "inactive_or_mismatched_fence", "The exact execution fence is not active."
            )
        self._provider(intent.provider_id)
        if intent.network_policy is NetworkPolicy.PUBLIC:
            if (
                self.outer_containment is None
                or intent.outer_containment_id != self.outer_containment.boundary_id
                or intent.outer_containment_digest
                != self.outer_containment.boundary_digest
            ):
                raise AttemptBlockedError(
                    "outer_containment_binding_mismatch",
                    "PUBLIC execution does not match the adapter's verified outer boundary.",
                )
        elif self.outer_containment is not None and (
            intent.outer_containment_id is not None
            or intent.outer_containment_digest is not None
        ):
            raise ContractError(
                "unexpected_outer_containment_binding",
                "Denied networking cannot bind the public outer boundary.",
            )
        self.source_verifier.verify(intent)
        staged, manifest_path, manifest_sha256 = self._stage_inputs(session, intent)
        record = self.journal.create_attempt(
            session,
            intent,
            fence_id=fence_id,
            input_staging_root=str(self.input_stage_store_root / intent.attempt_id),
            bootstrap_manifest_path=str(manifest_path),
            bootstrap_manifest_sha256=manifest_sha256,
            staged_attachments=staged,
        )
        if record.state is AttemptState.PREPARED:
            self._ensure_attempt_writable_directory(
                self.scratch_store_root / intent.attempt_id
            )
            self._ensure_attempt_writable_directory(
                self.provider_output_root / intent.attempt_id
            )
        return record

    def dispatch(self, attempt_id: str) -> AttemptRecord:
        record = self.journal.get_attempt(attempt_id)
        if record.state is not AttemptState.PREPARED:
            if record.state in _ACTIVE_STATES or record.state is AttemptState.UNKNOWN:
                return self.reconcile(attempt_id)
            return record
        fence = self.journal.get_fence(record.fence_id)
        if not self.journal.is_fence_active(record.fence_id) or not fence.matches(record.intent):
            return self._append_simple(
                record,
                operation_key=f"dispatch_fenced:{attempt_id}",
                event_type="fenced_before_effect",
                state=AttemptState.FENCED,
                effect_certainty=ProviderEffectCertainty.NONE,
            )
        self.source_verifier.verify(record.intent)
        self._verify_staged_inputs(record)
        self._ensure_attempt_writable_directory(
            self.scratch_store_root / record.attempt_id
        )
        self._ensure_attempt_writable_directory(
            self.provider_output_root / record.attempt_id
        )
        self.journal.append_event(
            attempt_id=attempt_id,
            operation_key=f"launch_requested:{attempt_id}",
            event_type="launch_requested",
            state=AttemptState.LAUNCH_REQUESTED,
            effect_certainty=ProviderEffectCertainty.UNKNOWN,
        )
        record = self.journal.get_attempt(attempt_id)
        self._inject("after_launch_requested", record)
        # The exact mutable boundaries are reverified after the effect intent is durable.
        self.source_verifier.verify(record.intent)
        self._verify_staged_inputs(record)
        request = self._launch_request(record)
        try:
            observation = self._provider(record.intent.provider_id).launch(request)
        except ProviderRejectedError as exc:
            self.journal.append_event(
                attempt_id=attempt_id,
                operation_key=f"launch_rejected:{attempt_id}",
                event_type="provider_rejected_before_effect",
                state=AttemptState.FAILED,
                detail_code=exc.code,
                effect_certainty=ProviderEffectCertainty.NONE,
            )
            return self.journal.get_attempt(attempt_id)
        except ProviderEffectUnknownError as exc:
            return self._record_unknown(record, "launch", exc.code)
        except Exception:
            return self._record_unknown(record, "launch", "provider_launch_exception")
        self._inject("after_provider_launch", record)
        return self._apply_observation(record, observation, operation="launch")

    def request_cancel(self, attempt_id: str) -> AttemptRecord:
        record = self.journal.get_attempt(attempt_id)
        if record.state is AttemptState.PREPARED:
            return self._append_simple(
                record,
                operation_key=f"cancel_without_effect:{attempt_id}",
                event_type="cancelled_before_effect",
                state=AttemptState.CANCELLED,
                effect_certainty=ProviderEffectCertainty.NONE,
            )
        if record.state is AttemptState.UNKNOWN:
            raise AttemptBlockedError(
                "unknown_requires_reconciliation",
                "UNKNOWN permits reconciliation only; cancellation is not replayed.",
            )
        if record.state not in _ACTIVE_STATES:
            return record
        if self.journal.event_for_operation(f"cancel_requested:{attempt_id}") is not None:
            return self.reconcile(attempt_id)
        self.journal.append_event(
            attempt_id=attempt_id,
            operation_key=f"cancel_requested:{attempt_id}",
            event_type="cancel_requested",
            state=AttemptState.CANCEL_REQUESTED,
            provider_ref=record.provider_ref,
            effect_certainty=ProviderEffectCertainty.KNOWN,
        )
        record = self.journal.get_attempt(attempt_id)
        self._inject("after_cancel_requested", record)
        try:
            observation = self._provider(record.intent.provider_id).request_cancel(
                self._provider_request(record)
            )
        except ProviderRejectedError as exc:
            self.journal.append_event(
                attempt_id=attempt_id,
                operation_key=f"cancel_rejected:{attempt_id}",
                event_type="cancel_rejected",
                state=AttemptState.CANCEL_REQUESTED,
                provider_ref=record.provider_ref,
                detail_code=exc.code,
                effect_certainty=ProviderEffectCertainty.KNOWN,
            )
            return self.journal.get_attempt(attempt_id)
        except ProviderEffectUnknownError as exc:
            return self._record_unknown(record, "cancel", exc.code)
        except Exception:
            return self._record_unknown(record, "cancel", "provider_cancel_exception")
        return self._apply_observation(record, observation, operation="cancel")

    def force_stop(self, attempt_id: str) -> AttemptRecord:
        record = self.journal.get_attempt(attempt_id)
        if record.state is AttemptState.PREPARED:
            return self._append_simple(
                record,
                operation_key=f"force_stop_without_effect:{attempt_id}",
                event_type="force_stopped_before_effect",
                state=AttemptState.FORCE_STOPPED,
                effect_certainty=ProviderEffectCertainty.NONE,
            )
        if record.state is AttemptState.UNKNOWN:
            raise AttemptBlockedError(
                "unknown_requires_reconciliation",
                "UNKNOWN permits reconciliation only; force-stop is not guessed.",
            )
        if record.state not in _ACTIVE_STATES:
            return record
        if self.journal.event_for_operation(f"force_stop_requested:{attempt_id}") is not None:
            return self.reconcile(attempt_id)
        self.journal.append_event(
            attempt_id=attempt_id,
            operation_key=f"force_stop_requested:{attempt_id}",
            event_type="force_stop_requested",
            state=AttemptState.FORCE_STOP_REQUESTED,
            provider_ref=record.provider_ref,
            effect_certainty=ProviderEffectCertainty.KNOWN,
        )
        record = self.journal.get_attempt(attempt_id)
        self._inject("after_force_stop_requested", record)
        try:
            observation = self._provider(record.intent.provider_id).force_stop(
                self._provider_request(record)
            )
        except ProviderRejectedError as exc:
            self.journal.append_event(
                attempt_id=attempt_id,
                operation_key=f"force_stop_rejected:{attempt_id}",
                event_type="force_stop_rejected",
                state=AttemptState.FORCE_STOP_REQUESTED,
                provider_ref=record.provider_ref,
                detail_code=exc.code,
                effect_certainty=ProviderEffectCertainty.KNOWN,
            )
            return self.journal.get_attempt(attempt_id)
        except ProviderEffectUnknownError as exc:
            return self._record_unknown(record, "force_stop", exc.code)
        except Exception:
            return self._record_unknown(record, "force_stop", "provider_force_stop_exception")
        return self._apply_observation(record, observation, operation="force_stop")

    def reconcile(self, attempt_id: str) -> AttemptRecord:
        record = self.journal.get_attempt(attempt_id)
        if record.state not in _ACTIVE_STATES and record.state is not AttemptState.UNKNOWN:
            return record
        try:
            observation = self._provider(record.intent.provider_id).reconcile(
                self._provider_request(record)
            )
        except ProviderEffectUnknownError as exc:
            return self._record_unknown(record, "reconcile", exc.code)
        except Exception:
            return self._record_unknown(record, "reconcile", "provider_reconcile_exception")
        return self._apply_observation(record, observation, operation="reconcile")

    def recover(self) -> tuple[AttemptRecord, ...]:
        """Reconcile durable effects only. Recovery never calls provider.launch."""

        recovered: list[AttemptRecord] = []
        for record in self.journal.requiring_reconciliation():
            recovered.append(self.reconcile(record.attempt_id))
        return tuple(recovered)

    def ingest_observation(
        self, attempt_id: str, observation: ProviderObservation
    ) -> AttemptRecord:
        return self._apply_observation(
            self.journal.get_attempt(attempt_id), observation, operation="ingress"
        )

    def revoke_fence(self, fence_id: str) -> tuple[AttemptRecord, ...]:
        self.journal.revoke_fence(fence_id)
        affected: list[AttemptRecord] = []
        for record in self.journal.attempts_for_fence(fence_id):
            if record.state is AttemptState.PREPARED:
                affected.append(
                    self._append_simple(
                        record,
                        operation_key=f"fence_revoked:{record.attempt_id}",
                        event_type="fenced_before_effect",
                        state=AttemptState.FENCED,
                        effect_certainty=ProviderEffectCertainty.NONE,
                    )
                )
            elif record.state in _ACTIVE_STATES:
                cancelled = self.request_cancel(record.attempt_id)
                self.journal.append_event(
                    attempt_id=record.attempt_id,
                    operation_key=f"fence_revoked:{record.attempt_id}",
                    event_type="fenced_after_effect",
                    state=AttemptState.FENCED,
                    provider_ref=cancelled.provider_ref,
                    effect_certainty=ProviderEffectCertainty.KNOWN,
                )
                affected.append(self.journal.get_attempt(record.attempt_id))
            elif record.state is AttemptState.UNKNOWN:
                affected.append(record)
        return tuple(affected)

    def verify_result(self, attempt_id: str) -> AttemptResult:
        record = self.journal.get_attempt(attempt_id)
        self._verify_staged_inputs(record)
        events = self.journal.list_events(attempt_id)
        result_artifacts: list[ResultArtifact] = []
        resources: list[ResourceObservation] = []
        for event in events:
            for artifact in event.artifacts:
                self._verify_sealed_artifact(record, artifact)
                result_artifacts.append(
                    ResultArtifact(
                        event_sequence=event.sequence,
                        evidence_only=event.evidence_only,
                        artifact=artifact,
                    )
                )
            if event.resource_facts:
                resources.append(
                    ResourceObservation(
                        event_sequence=event.sequence,
                        evidence_only=event.evidence_only,
                        facts=event.resource_facts,
                    )
                )
        certainty = ProviderEffectCertainty.NONE
        trusted = tuple(event for event in events if not event.evidence_only)
        if record.state is AttemptState.UNKNOWN:
            certainty = ProviderEffectCertainty.UNKNOWN
        elif any(
            event.effect_certainty is ProviderEffectCertainty.KNOWN for event in trusted
        ):
            certainty = ProviderEffectCertainty.KNOWN
        elif any(
            event.effect_certainty is ProviderEffectCertainty.UNKNOWN for event in trusted
        ):
            certainty = ProviderEffectCertainty.UNKNOWN
        containment = (
            self.outer_containment
            if record.intent.network_policy is NetworkPolicy.PUBLIC
            else None
        )
        return AttemptResult(
            session_id=record.session_id,
            session_digest=record.session.digest,
            attempt_id=record.attempt_id,
            attempt_intent_digest=record.intent.digest,
            previous_attempt_id=record.intent.previous_attempt_id,
            source_commit=record.intent.source_commit,
            source_digest=record.intent.source_digest,
            provider_id=record.intent.provider_id,
            provider_profile=record.intent.provider_profile,
            model_profile=record.intent.model_profile,
            reasoning_effort=record.intent.reasoning_effort,
            fence_id=record.fence_id,
            attempt_state=record.state,
            provider_effect_certainty=certainty,
            provider_ref=record.provider_ref,
            requested_resources=record.intent.resource_request,
            resource_observations=tuple(resources),
            bootstrap_manifest_sha256=record.bootstrap_manifest_sha256,
            staged_inventory_sha256=staged_inventory_digest(record.staged_attachments),
            artifacts=tuple(result_artifacts),
            event_history_sha256=event_history_digest(events),
            public_secretless_containment=containment,
        )

    def verify_sealed_artifacts(self, attempt_id: str) -> tuple[OutputArtifact, ...]:
        result = self.verify_result(attempt_id)
        return tuple(item.artifact for item in result.artifacts)

    def _append_simple(
        self,
        record: AttemptRecord,
        *,
        operation_key: str,
        event_type: str,
        state: AttemptState,
        effect_certainty: ProviderEffectCertainty,
    ) -> AttemptRecord:
        self.journal.append_event(
            attempt_id=record.attempt_id,
            operation_key=operation_key,
            event_type=event_type,
            state=state,
            provider_ref=record.provider_ref,
            effect_certainty=effect_certainty,
        )
        return self.journal.get_attempt(record.attempt_id)

    def _record_unknown(
        self, record: AttemptRecord, operation: str, detail_code: str
    ) -> AttemptRecord:
        self.journal.append_event(
            attempt_id=record.attempt_id,
            operation_key=f"{operation}_unknown:{record.attempt_id}:{detail_code}",
            event_type=f"{operation}_unknown",
            state=AttemptState.UNKNOWN,
            provider_ref=record.provider_ref,
            detail_code=detail_code,
            effect_certainty=ProviderEffectCertainty.UNKNOWN,
        )
        return self.journal.get_attempt(record.attempt_id)

    def _apply_observation(
        self,
        record: AttemptRecord,
        observation: ProviderObservation,
        *,
        operation: str,
    ) -> AttemptRecord:
        if (
            observation.provider_id != record.intent.provider_id
            or (
                record.provider_ref is not None
                and observation.provider_ref != record.provider_ref
            )
        ):
            self.journal.append_event(
                attempt_id=record.attempt_id,
                operation_key=f"{operation}_identity_mismatch:{record.attempt_id}",
                event_type="provider_identity_mismatch",
                state=AttemptState.UNKNOWN,
                detail_code="provider_identity_mismatch",
                effect_certainty=ProviderEffectCertainty.UNKNOWN,
            )
            return self.journal.get_attempt(record.attempt_id)
        evidence_only = record.state in {AttemptState.UNKNOWN, AttemptState.FENCED}
        artifacts = self._seal_observation_artifacts(record, observation)
        if evidence_only:
            state = record.state
        elif observation.state is ProviderState.RUNNING:
            state = (
                record.state
                if record.state
                in {AttemptState.CANCEL_REQUESTED, AttemptState.FORCE_STOP_REQUESTED}
                else AttemptState.RUNNING
            )
        elif observation.state is ProviderState.SUCCEEDED:
            state = AttemptState.SUCCEEDED
        elif observation.state is ProviderState.FAILED:
            state = AttemptState.FAILED
        elif observation.state is ProviderState.CANCELLED:
            state = AttemptState.CANCELLED
        elif observation.state is ProviderState.FORCE_STOPPED:
            state = AttemptState.FORCE_STOPPED
        else:
            state = AttemptState.UNKNOWN
        certainty = (
            ProviderEffectCertainty.KNOWN
            if observation.state
            in {
                ProviderState.RUNNING,
                ProviderState.SUCCEEDED,
                ProviderState.FAILED,
                ProviderState.CANCELLED,
                ProviderState.FORCE_STOPPED,
            }
            else ProviderEffectCertainty.UNKNOWN
        )
        digest = sha256_bytes(canonical_json_bytes(observation.as_dict()))
        self.journal.append_event(
            attempt_id=record.attempt_id,
            operation_key=f"{operation}_observation:{record.attempt_id}:{digest}",
            event_type=f"provider_{observation.state.value}",
            state=state,
            provider_ref=observation.provider_ref,
            provider_state=observation.state,
            detail_code=observation.detail_code,
            evidence_only=evidence_only,
            effect_certainty=certainty,
            exit_code=observation.exit_code,
            artifacts=artifacts,
            resource_facts=dict(observation.resource_facts),
        )
        return self.journal.get_attempt(record.attempt_id)

    def _launch_request(self, record: AttemptRecord) -> LaunchRequest:
        spec = self._execution_spec(record)
        bootstrap = canonical_json_bytes(
            {
                "schema": "wc.formal_attempt_bootstrap.v2",
                "attempt_id": record.attempt_id,
                "session_id": record.session_id,
                "manifest_path": record.bootstrap_manifest_path,
                "output_root": spec.output_directory,
                "scratch_root": spec.scratch_directory,
                "instruction": (
                    "Read the immutable manifest and its staged files. Write mathematical "
                    "deliverables under the exact output root; use the exact scratch root "
                    "only for intermediate or operational material for this Session."
                ),
            }
        )
        return LaunchRequest(
            attempt_id=record.attempt_id,
            session_id=record.session_id,
            fence_id=record.fence_id,
            execution_spec=spec,
            bootstrap_bytes=bootstrap,
            bootstrap_sha256=sha256_bytes(bootstrap),
        )

    def _provider_request(self, record: AttemptRecord) -> ProviderRequest:
        spec = self._execution_spec(record)
        return ProviderRequest(
            attempt_id=record.attempt_id,
            session_id=record.session_id,
            provider_ref=record.provider_ref,
            execution_spec_sha256=spec.digest,
            output_directory=spec.output_directory,
            scratch_directory=spec.scratch_directory,
            input_staging_root=spec.input_staging_root,
            bootstrap_manifest_path=spec.bootstrap_manifest_path,
            bootstrap_manifest_sha256=spec.bootstrap_manifest_sha256,
            staged_attachments=spec.staged_attachments,
        )

    def _execution_spec(self, record: AttemptRecord) -> ProviderExecutionSpec:
        return ProviderExecutionSpec(
            attempt_id=record.attempt_id,
            session_id=record.session_id,
            session_digest=record.session.digest,
            fence_id=record.fence_id,
            provider_id=record.intent.provider_id,
            provider_profile=record.intent.provider_profile,
            model_profile=record.intent.model_profile,
            reasoning_effort=record.intent.reasoning_effort,
            sandbox_policy=SandboxPolicy.FORMAL_WORKSPACE,
            network_policy=record.intent.network_policy,
            outer_containment=(
                self.outer_containment
                if record.intent.network_policy is NetworkPolicy.PUBLIC
                else None
            ),
            resource_request=record.intent.resource_request,
            baseline_capabilities=record.intent.baseline_capabilities,
            source_working_directory=record.intent.working_directory,
            scratch_directory=str(self.scratch_store_root / record.attempt_id),
            output_directory=str(self.provider_output_root / record.attempt_id),
            input_staging_root=record.input_staging_root,
            bootstrap_manifest_path=record.bootstrap_manifest_path,
            bootstrap_manifest_sha256=record.bootstrap_manifest_sha256,
            staged_attachments=record.staged_attachments,
            protected_root=None if self.protected_root is None else str(self.protected_root),
            selected_capabilities=record.intent.selected_capabilities,
        )

    def _stage_inputs(
        self, session: FormalSessionBinding, intent: AttemptIntent
    ) -> tuple[tuple[StagedInputAttachment, ...], Path, str]:
        for attachment in intent.source_attachments:
            self._require_authorized_source(attachment)
        root = self.input_stage_store_root / intent.attempt_id
        if root.exists():
            # A crash before the journal insert may leave one exact reusable stage.
            for attachment in intent.source_attachments:
                self._verify_regular_file(
                    Path(attachment.source_path),
                    expected_sha256=attachment.sha256,
                    expected_size=attachment.byte_length,
                    role="source_attachment",
                    require_single_link=False,
                )
            staged = tuple(
                StagedInputAttachment(
                    logical_name=item.logical_name,
                    staged_path=staged_attachment_path(str(root), item.logical_name),
                    sha256=item.sha256,
                    byte_length=item.byte_length,
                    media_type=item.media_type,
                    encoding=item.encoding,
                )
                for item in intent.source_attachments
            )
            manifest_path = root / "bootstrap.manifest.json"
            expected = self._manifest_bytes(session, intent, staged)
            if not manifest_path.is_file() or sha256_file(manifest_path) != sha256_bytes(expected):
                raise ContractError(
                    "staging_collision", "Existing staged input is not the exact Attempt stage."
                )
            temporary = AttemptRecord(
                session=session,
                intent=intent,
                fence_id="temporary",
                state=AttemptState.PREPARED,
                provider_ref=None,
                created_at="temporary",
                last_event_at="temporary",
                event_count=0,
                input_staging_root=str(root),
                bootstrap_manifest_path=str(manifest_path),
                bootstrap_manifest_sha256=sha256_bytes(expected),
                staged_attachments=staged,
            )
            self._verify_staged_inputs(temporary)
            return staged, manifest_path, sha256_bytes(expected)
        root.mkdir(mode=0o700)
        staged_items: list[StagedInputAttachment] = []
        try:
            for attachment in intent.source_attachments:
                destination = Path(staged_attachment_path(str(root), attachment.logical_name))
                self._copy_verified_input(attachment, destination)
                staged_items.append(
                    StagedInputAttachment(
                        logical_name=attachment.logical_name,
                        staged_path=str(destination),
                        sha256=attachment.sha256,
                        byte_length=attachment.byte_length,
                        media_type=attachment.media_type,
                        encoding=attachment.encoding,
                    )
                )
            staged = tuple(staged_items)
            manifest_bytes = self._manifest_bytes(session, intent, staged)
            manifest_path = root / "bootstrap.manifest.json"
            self._write_exclusive(manifest_path, manifest_bytes)
            self._make_stage_read_only(root)
            return staged, manifest_path, sha256_bytes(manifest_bytes)
        except BaseException:
            # Preserve a stage on an injected process-crash analogue; ordinary
            # failures remove only this newly-created exact Attempt directory.
            if not isinstance(__import__("sys").exc_info()[1], InjectedCrash):
                shutil.rmtree(root, ignore_errors=True)
            raise

    @staticmethod
    def _manifest_bytes(
        session: FormalSessionBinding,
        intent: AttemptIntent,
        staged: tuple[StagedInputAttachment, ...],
    ) -> bytes:
        return canonical_json_bytes(
            {
                "schema": "wc.formal_context_manifest.v1",
                "session_id": session.session_id,
                "session_digest": session.digest,
                "attempt_id": intent.attempt_id,
                "attempt_intent_sha256": intent.digest,
                "mission_ref": session.mission_ref,
                "strategy_ref": session.strategy_ref,
                "selected_bet_sha256": session.selected_bet_sha256,
                "context_ref": session.context_ref,
                "files": [
                    {
                        **item.as_dict(include_path=False),
                        "staged_relative_path": item.logical_name,
                    }
                    for item in staged
                ],
            }
        )

    def _verify_staged_inputs(self, record: AttemptRecord) -> None:
        root = Path(record.input_staging_root)
        self._require_directory_without_links(root, "input_staging_root")
        manifest = Path(record.bootstrap_manifest_path)
        self._verify_regular_file(
            manifest,
            expected_sha256=record.bootstrap_manifest_sha256,
            expected_size=None,
            role="bootstrap_manifest",
            require_single_link=True,
        )
        expected_paths = {str(manifest.absolute()).casefold()}
        for item in record.staged_attachments:
            expected = Path(staged_attachment_path(str(root), item.logical_name))
            if Path(item.staged_path) != expected:
                raise ContractError(
                    "attachment_staging_layout_mismatch", "Staged attachment path drifted."
                )
            self._verify_regular_file(
                expected,
                expected_sha256=item.sha256,
                expected_size=item.byte_length,
                role="staged_attachment",
                require_single_link=True,
            )
            expected_paths.add(str(expected.absolute()).casefold())
        actual_paths: set[str] = set()
        for directory, directories, files in os.walk(root, followlinks=False):
            for name in directories:
                self._require_directory_without_links(Path(directory) / name, "staged_directory")
            for name in files:
                actual_paths.add(str((Path(directory) / name).absolute()).casefold())
        if actual_paths != expected_paths:
            raise ContractError(
                "staging_inventory_mismatch", "Staged root contains missing or extra files."
            )

    def _copy_verified_input(self, attachment: SourceAttachment, destination: Path) -> None:
        source = Path(attachment.source_path)
        before = self._verify_regular_file(
            source,
            expected_sha256=attachment.sha256,
            expected_size=attachment.byte_length,
            role="source_attachment",
            require_single_link=False,
        )
        self._ensure_parent_directories(destination.parent, self.input_stage_store_root)
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        descriptor = os.open(destination, flags, 0o600)
        try:
            with source.open("rb") as reader, os.fdopen(descriptor, "wb") as writer:
                descriptor = -1
                shutil.copyfileobj(reader, writer, length=1024 * 1024)
                writer.flush()
                os.fsync(writer.fileno())
        finally:
            if descriptor >= 0:
                os.close(descriptor)
        after = self._verify_regular_file(
            source,
            expected_sha256=attachment.sha256,
            expected_size=attachment.byte_length,
            role="source_attachment",
            require_single_link=False,
        )
        if not self._same_file_identity(before, after):
            raise ContractError(
                "source_attachment_changed", "Source attachment changed during staging."
            )
        self._verify_regular_file(
            destination,
            expected_sha256=attachment.sha256,
            expected_size=attachment.byte_length,
            role="staged_attachment",
            require_single_link=True,
        )

    def _seal_observation_artifacts(
        self, record: AttemptRecord, observation: ProviderObservation
    ) -> tuple[OutputArtifact, ...]:
        output_root = self.provider_output_root / record.attempt_id
        scratch_root = self.scratch_store_root / record.attempt_id
        seal_root = self.artifact_store_root / record.attempt_id
        roots = {"output": output_root, "scratch": scratch_root}
        for role, root in roots.items():
            self._require_directory_without_links(root, f"provider_{role}")
        if observation.state in {
            ProviderState.SUCCEEDED,
            ProviderState.FAILED,
            ProviderState.CANCELLED,
            ProviderState.FORCE_STOPPED,
        }:
            actual_paths: set[str] = set()
            actual_identities: set[tuple[int, int]] = set()
            for role, root in roots.items():
                for directory, directories, files in os.walk(root, followlinks=False):
                    for name in directories:
                        self._require_directory_without_links(
                            Path(directory) / name, f"provider_{role}_directory"
                        )
                    for name in files:
                        path = Path(directory) / name
                        self._require_no_link_components(path, f"provider_{role}_file")
                        details = path.lstat()
                        if (
                            self._is_link_or_reparse(details)
                            or not stat.S_ISREG(details.st_mode)
                            or details.st_nlink != 1
                        ):
                            raise ContractError(
                                "provider_artifact_inventory_unsafe",
                                "Terminal provider custody contains an unsafe filesystem object.",
                            )
                        identity = (details.st_dev, details.st_ino)
                        if identity in actual_identities:
                            raise ContractError(
                                "provider_artifact_inventory_alias",
                                "Terminal provider custody contains aliased file identities.",
                            )
                        actual_identities.add(identity)
                        actual_paths.add(os.path.normcase(str(path.absolute())))
            reported_paths = {
                os.path.normcase(str(Path(item.path).absolute()))
                for item in observation.artifacts
            }
            if (
                len(reported_paths) != len(observation.artifacts)
                or reported_paths != actual_paths
            ):
                raise ContractError(
                    "provider_artifact_inventory_incomplete",
                    "Terminal provider observation does not enumerate exact output and scratch custody.",
                )
        if not observation.artifacts:
            return ()
        if seal_root.exists():
            self._require_directory_without_links(seal_root, "artifact_seal")
        else:
            seal_root.mkdir(mode=0o700)
        result: list[OutputArtifact] = []
        for artifact in observation.artifacts:
            source = Path(artifact.path)
            custody_root = artifact.custody_root or "output"
            source_root = roots.get(custody_root)
            if source_root is None or not self._path_within(source, source_root):
                raise ContractError(
                    "artifact_outside_provider_custody",
                    "Provider artifact is outside its exact Attempt custody root.",
                )
            relative_path = source.relative_to(source_root).as_posix()
            if artifact.relative_path is not None and artifact.relative_path != relative_path:
                raise ContractError(
                    "artifact_relative_path_mismatch",
                    "Provider artifact relative path does not match its exact source path.",
                )
            before = self._verify_regular_file(
                source,
                expected_sha256=artifact.sha256,
                expected_size=artifact.size_bytes,
                role="provider_artifact",
                require_single_link=True,
            )
            destination = seal_root / artifact.sha256
            if destination.exists():
                self._verify_regular_file(
                    destination,
                    expected_sha256=artifact.sha256,
                    expected_size=artifact.size_bytes,
                    role="sealed_artifact",
                    require_single_link=True,
                )
            else:
                self._copy_file_exclusive(source, destination)
                os.chmod(destination, stat.S_IREAD)
            after = self._verify_regular_file(
                source,
                expected_sha256=artifact.sha256,
                expected_size=artifact.size_bytes,
                role="provider_artifact",
                require_single_link=True,
            )
            if not self._same_file_identity(before, after):
                raise ContractError(
                    "provider_artifact_changed", "Provider artifact changed during sealing."
                )
            result.append(
                OutputArtifact(
                    name=artifact.name,
                    path=str(destination),
                    sha256=artifact.sha256,
                    size_bytes=artifact.size_bytes,
                    media_type=artifact.media_type,
                    encoding=artifact.encoding,
                    custody_root=custody_root,
                    relative_path=relative_path,
                )
            )
        return tuple(result)

    def _verify_sealed_artifact(
        self, record: AttemptRecord, artifact: OutputArtifact
    ) -> None:
        seal_root = self.artifact_store_root / record.attempt_id
        if not self._path_within(Path(artifact.path), seal_root):
            raise ContractError(
                "sealed_artifact_outside_store", "Journal artifact is outside the seal store."
            )
        if Path(artifact.path).name != artifact.sha256:
            raise ContractError(
                "sealed_artifact_address_mismatch",
                "Sealed artifact path is not its content address.",
            )
        self._verify_regular_file(
            Path(artifact.path),
            expected_sha256=artifact.sha256,
            expected_size=artifact.size_bytes,
            role="sealed_artifact",
            require_single_link=True,
        )

    def _require_authorized_source(self, attachment: SourceAttachment) -> None:
        source = self._normalize_exact_root(attachment.source_path)
        for root in self.source_attachment_roots:
            self._require_directory_without_links(root, "source_attachment_root")
            if self._path_within(source, root):
                return
        raise ContractError(
            "source_attachment_not_authorized",
            "Source attachments must be strictly beneath a configured trusted source root.",
        )

    @classmethod
    def _verify_regular_file(
        cls,
        path: Path,
        *,
        expected_sha256: str,
        expected_size: int | None,
        role: str,
        require_single_link: bool,
    ) -> os.stat_result:
        cls._require_no_link_components(path, role)
        try:
            details = path.lstat()
        except OSError as exc:
            raise ContractError("file_unavailable", f"{role} is unavailable.") from exc
        if cls._is_link_or_reparse(details) or not stat.S_ISREG(details.st_mode):
            raise ContractError("unsafe_file_type", f"{role} must be a regular non-link file.")
        if require_single_link and details.st_nlink != 1:
            raise ContractError("unsafe_hardlink", f"{role} must have one hard link.")
        if expected_size is not None and details.st_size != expected_size:
            raise ContractError("file_size_mismatch", f"{role} size does not match custody facts.")
        if sha256_file(path) != expected_sha256:
            raise ContractError("file_digest_mismatch", f"{role} digest does not match custody facts.")
        return details

    @classmethod
    def _require_no_link_components(cls, path: Path, role: str) -> None:
        current = Path(path.anchor)
        for part in path.parts[1:]:
            current /= part
            if not current.exists() and not current.is_symlink():
                continue
            details = current.lstat()
            if cls._is_link_or_reparse(details):
                raise ContractError("unsafe_path_component", f"{role} contains a link/reparse point.")

    @classmethod
    def _require_directory_without_links(cls, path: Path, role: str) -> None:
        cls._require_no_link_components(path, role)
        try:
            details = path.lstat()
        except OSError as exc:
            raise ContractError("directory_unavailable", f"{role} is unavailable.") from exc
        if cls._is_link_or_reparse(details) or not stat.S_ISDIR(details.st_mode):
            raise ContractError("unsafe_directory", f"{role} must be a non-link directory.")

    @classmethod
    def _ensure_owned_directory(cls, path: Path) -> None:
        path.mkdir(parents=True, exist_ok=True)
        cls._require_directory_without_links(path, "owned_store")

    @classmethod
    def _ensure_attempt_writable_directory(cls, path: Path) -> None:
        if path.exists():
            cls._require_directory_without_links(path, "attempt_writable_directory")
            if any(path.iterdir()):
                raise ContractError(
                    "attempt_writable_directory_collision",
                    "A prepared Attempt writable directory must be empty.",
                )
            return
        path.mkdir(mode=0o700)

    @classmethod
    def _ensure_parent_directories(cls, path: Path, stop: Path) -> None:
        relative = path.relative_to(stop)
        current = stop
        for part in relative.parts:
            current /= part
            if current.exists():
                cls._require_directory_without_links(current, "staged_directory")
            else:
                current.mkdir(mode=0o700)

    @staticmethod
    def _write_exclusive(path: Path, content: bytes) -> None:
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        descriptor = os.open(path, flags, 0o600)
        try:
            with os.fdopen(descriptor, "wb") as handle:
                descriptor = -1
                handle.write(content)
                handle.flush()
                os.fsync(handle.fileno())
        finally:
            if descriptor >= 0:
                os.close(descriptor)

    @staticmethod
    def _copy_file_exclusive(source: Path, destination: Path) -> None:
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        descriptor = os.open(destination, flags, 0o600)
        try:
            with source.open("rb") as reader, os.fdopen(descriptor, "wb") as writer:
                descriptor = -1
                shutil.copyfileobj(reader, writer, length=1024 * 1024)
                writer.flush()
                os.fsync(writer.fileno())
        finally:
            if descriptor >= 0:
                os.close(descriptor)

    @classmethod
    def _make_stage_read_only(cls, root: Path) -> None:
        for directory, directories, files in os.walk(root, topdown=False):
            for name in files:
                os.chmod(Path(directory) / name, stat.S_IREAD)
            for name in directories:
                os.chmod(Path(directory) / name, stat.S_IREAD | stat.S_IEXEC)
        os.chmod(root, stat.S_IREAD | stat.S_IEXEC)

    @staticmethod
    def _paths_overlap(first: Path, second: Path) -> bool:
        first_abs = Path(os.path.normcase(os.path.abspath(first)))
        second_abs = Path(os.path.normcase(os.path.abspath(second)))
        return first_abs == second_abs or first_abs in second_abs.parents or second_abs in first_abs.parents

    @staticmethod
    def _normalize_exact_root(path: str | Path) -> Path:
        candidate = Path(path)
        if not candidate.is_absolute():
            raise ContractError("invalid_path", "Trusted roots must be absolute paths.")
        return Path(os.path.abspath(candidate))

    @staticmethod
    def _path_within(path: Path, root: Path) -> bool:
        path_abs = Path(os.path.normcase(os.path.abspath(path)))
        root_abs = Path(os.path.normcase(os.path.abspath(root)))
        return path_abs != root_abs and root_abs in path_abs.parents

    @staticmethod
    def _same_file_identity(first: os.stat_result, second: os.stat_result) -> bool:
        return (first.st_dev, first.st_ino, first.st_size) == (
            second.st_dev,
            second.st_ino,
            second.st_size,
        )

    @staticmethod
    def _is_link_or_reparse(details: os.stat_result) -> bool:
        reparse = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
        attributes = getattr(details, "st_file_attributes", 0)
        return stat.S_ISLNK(details.st_mode) or bool(attributes & reparse)
