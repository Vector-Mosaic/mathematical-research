from __future__ import annotations

import json
import os
import sqlite3
import stat
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from research_attempt_adapter import (
    AttemptBlockedError,
    AttemptIntent,
    AttemptJournal,
    AttemptState,
    ContractError,
    ExecutionFence,
    FormalSessionBinding,
    InjectedCrash,
    NetworkPolicy,
    McpServerSelection,
    OutputArtifact,
    ProviderEffectCertainty,
    ProviderObservation,
    ProviderState,
    ResearchAttemptAdapter,
    ResourceRequest,
    SelectedCapabilities,
    SourceAttachment,
    VerifiedOuterContainment,
    sha256_bytes,
    sha256_file,
)

from research_attempt_adapter.testing import (
    FakeAttemptProvider,
    FakeProviderBackend,
    FakeSourceVerifier,
)


def _digest(character: str) -> str:
    return character * 64


class ResearchAttemptAdapterTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.source_root = self.root / "source"
        self.source_root.mkdir()
        self.context_root = self.root / "context"
        self.context_root.mkdir()
        self.backend = FakeProviderBackend()
        self.provider = FakeAttemptProvider(self.backend)
        self.journal = AttemptJournal(self.root / "journal.sqlite3")
        self.session = FormalSessionBinding(
            session_id="session-1",
            session_digest=_digest("1"),
            mission_ref="mission/current",
            strategy_ref="strategy/current",
            selected_bet_sha256=_digest("2"),
            context_ref="context/revision/current",
        )
        self.fence = ExecutionFence(
            fence_id="fence-1",
            provider_id="fake_provider",
            coordination_id="coord-1",
            coordination_digest=_digest("3"),
            authorization_id="auth-1",
            authorization_digest=_digest("4"),
            source_commit="a" * 40,
            source_digest=_digest("5"),
        )
        self.adapter = self._adapter()
        self.adapter.install_fence(self.fence)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _adapter(
        self,
        *,
        fault=None,
        containment: VerifiedOuterContainment | None = None,
        protected_root: Path | None = None,
        source_roots: tuple[Path, ...] | None = None,
    ) -> ResearchAttemptAdapter:
        return ResearchAttemptAdapter(
            journal=self.journal,
            providers={"fake_provider": self.provider},
            source_verifier=FakeSourceVerifier(),
            input_stage_store_root=self.root / "staged",
            scratch_store_root=self.root / "scratch",
            provider_output_root=self.root / "output",
            artifact_store_root=self.root / "sealed",
            protected_root=protected_root,
            source_attachment_roots=(self.context_root,) if source_roots is None else source_roots,
            outer_containment=containment,
            fault_injector=fault,
        )

    def _attachment(self, name: str, content: bytes) -> SourceAttachment:
        path = self.context_root.joinpath(*name.split("/"))
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
        return SourceAttachment(
            logical_name=name,
            source_path=str(path),
            sha256=sha256_bytes(content),
            byte_length=len(content),
            media_type="text/plain",
            encoding="utf-8",
        )

    def _intent(
        self,
        attempt_id: str,
        *,
        attachments: tuple[SourceAttachment, ...] = (),
        previous: str | None = None,
        readiness: str = "6",
        resource_request: ResourceRequest | None = None,
        network_policy: NetworkPolicy = NetworkPolicy.DENIED,
        containment: VerifiedOuterContainment | None = None,
    ) -> AttemptIntent:
        return AttemptIntent(
            attempt_id=attempt_id,
            session_id=self.session.session_id,
            session_digest=self.session.digest,
            previous_attempt_id=previous,
            correction_basis=(None if previous is None else "provider readiness changed"),
            readiness_state_digest=_digest(readiness),
            coordination_id=self.fence.coordination_id,
            coordination_digest=self.fence.coordination_digest,
            authorization_id=self.fence.authorization_id,
            authorization_digest=self.fence.authorization_digest,
            source_commit=self.fence.source_commit,
            source_digest=self.fence.source_digest,
            provider_id=self.fence.provider_id,
            provider_profile="fake-provider.v2",
            model_profile="fake-model",
            reasoning_effort="ultra",
            resource_request=resource_request or ResourceRequest(),
            baseline_capabilities=("shell", "web_search")
            if network_policy is NetworkPolicy.PUBLIC
            else ("shell",),
            network_policy=network_policy,
            outer_containment_id=(None if containment is None else containment.boundary_id),
            outer_containment_digest=(None if containment is None else containment.boundary_digest),
            source_attachments=attachments,
            working_directory=str(self.source_root),
        )

    def test_zero_or_many_large_context_files_stage_without_repository_caps(self) -> None:
        no_source_adapter = self._adapter(source_roots=())
        empty = no_source_adapter.prepare(
            self.session,
            self._intent("attempt-empty"),
            fence_id=self.fence.fence_id,
        )
        self.assertEqual(empty.staged_attachments, ())
        attachments = tuple(
            self._attachment(f"packet/{index:03}.txt", bytes([65 + index % 20]) * 131_072)
            for index in range(40)
        )
        record = self.adapter.prepare(
            self.session,
            self._intent("attempt-many", attachments=attachments),
            fence_id=self.fence.fence_id,
        )
        self.assertEqual(len(record.staged_attachments), len(attachments))
        self.assertEqual(
            sum(item.byte_length for item in record.staged_attachments),
            sum(item.byte_length for item in attachments),
        )

    def test_only_constructor_authorized_source_is_staged(self) -> None:
        attachment = self._attachment("nested/context.txt", b"immutable context")
        source = Path(attachment.source_path)
        os.chmod(source, stat.S_IREAD)
        try:
            record = self.adapter.prepare(
                self.session,
                self._intent("attempt-authorized", attachments=(attachment,)),
                fence_id=self.fence.fence_id,
            )
        finally:
            os.chmod(source, stat.S_IREAD | stat.S_IWRITE)
        self.assertEqual(len(record.staged_attachments), 1)
        self.assertEqual(sha256_file(record.staged_attachments[0].staged_path), attachment.sha256)

    def test_protected_or_unauthorized_source_is_rejected_before_copy(self) -> None:
        protected_root = self.root / "protected"
        protected_root.mkdir()
        protected_file = protected_root / "credential.txt"
        protected_file.write_bytes(b"credential material")
        outside_file = self.root / "not-authorized.txt"
        outside_file.write_bytes(b"outside")
        adapter = self._adapter(
            protected_root=protected_root,
            source_roots=(self.context_root,),
        )
        for attempt_id, path in (
            ("attempt-protected-source", protected_file),
            ("attempt-unauthorized-source", outside_file),
        ):
            attachment = SourceAttachment(
                logical_name="context.txt",
                source_path=str(path),
                sha256=sha256_file(path),
                byte_length=path.stat().st_size,
                media_type="text/plain",
                encoding="utf-8",
            )
            with self.subTest(path=path), self.assertRaisesRegex(
                ContractError, "strictly beneath"
            ):
                adapter.prepare(
                    self.session,
                    self._intent(attempt_id, attachments=(attachment,)),
                    fence_id=self.fence.fence_id,
                )
            self.assertFalse((self.root / "staged" / attempt_id).exists())

    def test_trusted_source_roots_must_be_disjoint_from_owned_and_protected_roots(self) -> None:
        with self.assertRaisesRegex(ContractError, "Attempt-owned stores"):
            self._adapter(source_roots=(self.root / "staged",))

        protected_root = self.root / "protected-root"
        protected_source = protected_root / "context"
        protected_source.mkdir(parents=True)
        with self.assertRaisesRegex(ContractError, "protected root"):
            self._adapter(
                protected_root=protected_root,
                source_roots=(protected_source,),
            )

    def test_identity_length_and_selected_tool_count_have_no_repository_ceiling(self) -> None:
        intent = replace(
            self._intent("attempt-unbounded-contract"),
            attempt_id="attempt-" + "x" * 10_000,
            selected_capabilities=SelectedCapabilities(
                mcp_servers=(
                    McpServerSelection(
                        "formal-tools",
                        tuple(f"tool-{index}" for index in range(1_000)),
                    ),
                )
            ),
        )
        self.assertGreater(len(intent.attempt_id), 255)
        self.assertEqual(
            len(intent.selected_capabilities.mcp_servers[0].enabled_tools), 1_000
        )

    def test_portable_attachment_grammar_rejects_escape_reserved_and_case_collision(self) -> None:
        source = self.context_root / "safe.txt"
        source.write_bytes(b"safe")
        for logical_name in ("../escape.txt", "CON.txt", "trailing./x", "bad?.txt"):
            with self.subTest(logical_name=logical_name), self.assertRaises(ContractError):
                SourceAttachment(
                    logical_name=logical_name,
                    source_path=str(source),
                    sha256=sha256_file(source),
                    byte_length=source.stat().st_size,
                    media_type="text/plain",
                    encoding="utf-8",
                )
        upper = self._attachment("Case.txt", b"upper")
        lower = self._attachment("case.txt", b"lower")
        with self.assertRaisesRegex(ContractError, "portable-case unique"):
            replace(
                self._intent("attempt-case"),
                source_attachments=(upper, lower),
            )

    def test_staged_symlink_is_rejected_before_provider_effect(self) -> None:
        attachment = self._attachment("context.txt", b"context")
        record = self.adapter.prepare(
            self.session,
            self._intent("attempt-symlink", attachments=(attachment,)),
            fence_id=self.fence.fence_id,
        )
        staged = Path(record.staged_attachments[0].staged_path)
        os.chmod(staged.parent, stat.S_IWRITE | stat.S_IREAD | stat.S_IEXEC)
        os.chmod(staged, stat.S_IWRITE | stat.S_IREAD)
        staged.unlink()
        try:
            staged.symlink_to(Path(attachment.source_path))
        except OSError as exc:
            self.skipTest(f"symlink creation unavailable: {exc}")
        with self.assertRaises(ContractError):
            self.adapter.dispatch(record.attempt_id)
        self.assertEqual(self.backend.launch_calls, 0)

    def test_manifest_contains_only_opaque_session_binding_and_staged_inventory(self) -> None:
        secret_content = b"exact context body that must not enter bootstrap stdin"
        attachment = self._attachment("context.txt", secret_content)
        captured = []
        self.backend.on_launch = captured.append
        record = self.adapter.prepare(
            self.session,
            self._intent("attempt-manifest", attachments=(attachment,)),
            fence_id=self.fence.fence_id,
        )
        manifest_text = Path(record.bootstrap_manifest_path).read_text(encoding="utf-8")
        self.assertNotIn(attachment.source_path, manifest_text)
        self.assertNotIn(secret_content.decode(), manifest_text)
        self.assertIn(self.session.context_ref, manifest_text)
        self.adapter.dispatch(record.attempt_id)
        bootstrap = captured[0].bootstrap_bytes.decode("utf-8")
        self.assertNotIn(secret_content.decode(), bootstrap)
        self.assertNotIn(attachment.source_path, bootstrap)
        bootstrap_payload = json.loads(bootstrap)
        self.assertEqual(
            bootstrap_payload["manifest_path"], record.bootstrap_manifest_path
        )
        self.assertIn("mathematical deliverables", bootstrap_payload["instruction"])
        self.assertIn("intermediate or operational", bootstrap_payload["instruction"])

    def test_launch_request_is_durable_and_inputs_reverified_before_effect(self) -> None:
        attachment = self._attachment("context.txt", b"context")
        seen = []

        def on_launch(request) -> None:
            seen.append(self.journal.get_attempt(request.attempt_id).state)

        self.backend.on_launch = on_launch
        record = self.adapter.prepare(
            self.session,
            self._intent("attempt-launch", attachments=(attachment,)),
            fence_id=self.fence.fence_id,
        )
        dispatched = self.adapter.dispatch(record.attempt_id)
        self.assertEqual(seen, [AttemptState.LAUNCH_REQUESTED])
        self.assertEqual(dispatched.state, AttemptState.RUNNING)

    def test_mutation_after_launch_journal_blocks_before_provider_call(self) -> None:
        attachment = self._attachment("context.txt", b"original")

        def mutate(point: str, record) -> None:
            if point == "after_launch_requested":
                path = Path(record.staged_attachments[0].staged_path)
                os.chmod(path, stat.S_IWRITE | stat.S_IREAD)
                path.write_bytes(b"mutated!")

        adapter = self._adapter(fault=mutate)
        record = adapter.prepare(
            self.session,
            self._intent("attempt-mutated", attachments=(attachment,)),
            fence_id=self.fence.fence_id,
        )
        with self.assertRaises(ContractError):
            adapter.dispatch(record.attempt_id)
        self.assertEqual(self.backend.launch_calls, 0)
        self.assertEqual(self.journal.get_attempt(record.attempt_id).state, AttemptState.LAUNCH_REQUESTED)

    def test_recovery_after_append_before_effect_never_launches(self) -> None:
        def crash(point: str, _record) -> None:
            if point == "after_launch_requested":
                raise InjectedCrash()

        adapter = self._adapter(fault=crash)
        record = adapter.prepare(
            self.session,
            self._intent("attempt-crash"),
            fence_id=self.fence.fence_id,
        )
        with self.assertRaises(InjectedCrash):
            adapter.dispatch(record.attempt_id)
        self.assertEqual(self.backend.launch_calls, 0)
        recovered = self._adapter().recover()
        self.assertEqual(self.backend.launch_calls, 0)
        self.assertEqual(self.backend.reconcile_calls, 1)
        self.assertEqual(recovered[0].state, AttemptState.UNKNOWN)

    def test_crash_after_provider_launch_reconciles_without_duplicate_launch(self) -> None:
        def crash(point: str, _record) -> None:
            if point == "after_provider_launch":
                raise InjectedCrash()

        adapter = self._adapter(fault=crash)
        record = adapter.prepare(
            self.session,
            self._intent("attempt-after-launch"),
            fence_id=self.fence.fence_id,
        )
        with self.assertRaises(InjectedCrash):
            adapter.dispatch(record.attempt_id)
        self.assertEqual(self.backend.launch_calls, 1)
        recovered = self._adapter().recover()
        self.assertEqual(self.backend.launch_calls, 1)
        self.assertEqual(self.backend.reconcile_calls, 1)
        self.assertEqual(recovered[0].state, AttemptState.RUNNING)

    def test_unknown_blocks_retry(self) -> None:
        self.backend.next_launch_error = "unknown"
        first = self.adapter.prepare(
            self.session, self._intent("attempt-unknown"), fence_id=self.fence.fence_id
        )
        first = self.adapter.dispatch(first.attempt_id)
        self.assertEqual(first.state, AttemptState.UNKNOWN)
        with self.assertRaisesRegex(AttemptBlockedError, "Retry requires"):
            self.adapter.retry(
                first.attempt_id,
                self._intent(
                    "attempt-after-unknown",
                    previous=first.attempt_id,
                    readiness="7",
                ),
                fence_id=self.fence.fence_id,
            )

    def test_retry_requires_material_correction_and_has_no_chain_counter(self) -> None:
        first_intent = self._intent("attempt-retry-0")
        self.provider.set_observation(
            first_intent.attempt_id,
            ProviderObservation(
                provider_id="fake_provider",
                provider_ref=f"fake:{first_intent.attempt_id}",
                state=ProviderState.FAILED,
                detail_code="fake_failed",
            ),
        )
        previous = self.adapter.dispatch(
            self.adapter.prepare(self.session, first_intent, fence_id=self.fence.fence_id).attempt_id
        )
        unchanged = self._intent(
            "attempt-retry-unchanged",
            previous=previous.attempt_id,
            readiness="6",
        )
        with self.assertRaisesRegex(AttemptBlockedError, "unchanged"):
            self.adapter.retry(previous.attempt_id, unchanged, fence_id=self.fence.fence_id)
        for index, readiness in enumerate("789abcdef", start=1):
            intent = self._intent(
                f"attempt-retry-{index}",
                previous=previous.attempt_id,
                readiness=readiness,
            )
            self.provider.set_observation(
                intent.attempt_id,
                ProviderObservation(
                    provider_id="fake_provider",
                    provider_ref=f"fake:{intent.attempt_id}",
                    state=ProviderState.FAILED,
                    detail_code="fake_failed",
                ),
            )
            previous = self.adapter.retry(
                previous.attempt_id, intent, fence_id=self.fence.fence_id
            )
            previous = self.adapter.dispatch(previous.attempt_id)
            self.assertEqual(previous.state, AttemptState.FAILED)

    def test_graceful_cancel_and_explicit_force_stop_are_distinct_idempotent_effects(self) -> None:
        record = self.adapter.prepare(
            self.session, self._intent("attempt-stop"), fence_id=self.fence.fence_id
        )
        record = self.adapter.dispatch(record.attempt_id)
        cancelled = self.adapter.request_cancel(record.attempt_id)
        self.assertEqual(cancelled.state, AttemptState.CANCEL_REQUESTED)
        self.assertEqual(self.backend.cancel_calls, 1)
        self.assertEqual(self.backend.force_stop_calls, 0)
        self.adapter.request_cancel(record.attempt_id)
        self.assertEqual(self.backend.cancel_calls, 1)
        forced = self.adapter.force_stop(record.attempt_id)
        self.assertEqual(forced.state, AttemptState.FORCE_STOP_REQUESTED)
        self.assertEqual(self.backend.force_stop_calls, 1)
        self.adapter.force_stop(record.attempt_id)
        self.assertEqual(self.backend.force_stop_calls, 1)

    def test_public_network_must_match_independently_injected_containment(self) -> None:
        containment = VerifiedOuterContainment(
            boundary_id="outer-1",
            boundary_digest=_digest("8"),
            public_only_egress=True,
            private_network_denied=True,
            metadata_denied=True,
            inbound_denied=True,
        )
        intent = self._intent(
            "attempt-public",
            network_policy=NetworkPolicy.PUBLIC,
            containment=containment,
        )
        with self.assertRaisesRegex(AttemptBlockedError, "verified outer boundary"):
            self.adapter.prepare(self.session, intent, fence_id=self.fence.fence_id)
        matching = self._adapter(containment=containment)
        record = matching.prepare(self.session, intent, fence_id=self.fence.fence_id)
        self.assertEqual(record.state, AttemptState.PREPARED)

    def test_public_containment_fact_requires_every_denial_property(self) -> None:
        for field_name in (
            "public_only_egress",
            "private_network_denied",
            "metadata_denied",
            "inbound_denied",
        ):
            values = {
                "boundary_id": "outer-invalid",
                "boundary_digest": _digest("8"),
                "public_only_egress": True,
                "private_network_denied": True,
                "metadata_denied": True,
                "inbound_denied": True,
            }
            values[field_name] = False
            with self.subTest(field_name=field_name), self.assertRaises(ContractError):
                VerifiedOuterContainment(**values)

    def test_foreign_provider_identity_is_unknown_and_not_attributed(self) -> None:
        record = self.adapter.prepare(
            self.session, self._intent("attempt-foreign"), fence_id=self.fence.fence_id
        )
        record = self.adapter.dispatch(record.attempt_id)
        result = self.adapter.ingest_observation(
            record.attempt_id,
            ProviderObservation(
                provider_id="foreign_provider",
                provider_ref="foreign:attempt",
                state=ProviderState.SUCCEEDED,
                detail_code="foreign_success",
            ),
        )
        self.assertEqual(result.state, AttemptState.UNKNOWN)
        self.assertEqual(self.adapter.verify_result(record.attempt_id).artifacts, ())

    def test_artifact_is_content_addressed_and_fresh_tamper_check_fails(self) -> None:
        record = self.adapter.prepare(
            self.session, self._intent("attempt-artifact"), fence_id=self.fence.fence_id
        )
        output = self.root / "output" / record.attempt_id / "proof.txt"

        def produce(_request) -> None:
            output.write_bytes(b"raw factual output")
            self.provider.set_observation(
                record.attempt_id,
                ProviderObservation(
                    provider_id="fake_provider",
                    provider_ref=f"fake:{record.attempt_id}",
                    state=ProviderState.SUCCEEDED,
                    detail_code="fake_succeeded",
                    artifacts=(
                        OutputArtifact(
                            name="proof",
                            path=str(output),
                            sha256=sha256_file(output),
                            size_bytes=output.stat().st_size,
                            media_type="text/plain",
                            encoding="utf-8",
                        ),
                    ),
                    resource_facts={
                        "peak_memory_mb": 17,
                        "output_bytes": output.stat().st_size,
                    },
                ),
            )

        self.backend.on_launch = produce
        record = self.adapter.dispatch(record.attempt_id)
        result = self.adapter.verify_result(record.attempt_id)
        self.assertEqual(result.attempt_state, AttemptState.SUCCEEDED)
        self.assertEqual(result.provider_effect_certainty, ProviderEffectCertainty.KNOWN)
        self.assertEqual(result.artifacts[0].artifact.path.split(os.sep)[-1], sha256_file(output))
        seal = Path(result.artifacts[0].artifact.path)
        os.chmod(seal, stat.S_IWRITE | stat.S_IREAD)
        seal.write_bytes(b"tam factual output")
        with self.assertRaisesRegex(ContractError, "digest"):
            self.adapter.verify_result(record.attempt_id)

    def test_sealing_preserves_complete_output_and_scratch_origins(self) -> None:
        record = self.adapter.prepare(
            self.session, self._intent("attempt-both-roots"), fence_id=self.fence.fence_id
        )
        output = self.root / "output" / record.attempt_id / "reports" / "result.txt"
        scratch = self.root / "scratch" / record.attempt_id / "computations" / "trace.bin"

        def produce(_request) -> None:
            output.parent.mkdir(parents=True)
            scratch.parent.mkdir(parents=True)
            output.write_bytes(b"same bytes")
            scratch.write_bytes(b"same bytes")
            self.provider.set_observation(
                record.attempt_id,
                ProviderObservation(
                    provider_id="fake_provider",
                    provider_ref=f"fake:{record.attempt_id}",
                    state=ProviderState.SUCCEEDED,
                    detail_code="fake_succeeded",
                    artifacts=(
                        OutputArtifact(
                            name="output-result",
                            path=str(output),
                            sha256=sha256_file(output),
                            size_bytes=output.stat().st_size,
                            media_type="text/plain",
                            encoding="utf-8",
                            custody_root="output",
                            relative_path="reports/result.txt",
                        ),
                        OutputArtifact(
                            name="scratch-trace",
                            path=str(scratch),
                            sha256=sha256_file(scratch),
                            size_bytes=scratch.stat().st_size,
                            media_type="application/octet-stream",
                            custody_root="scratch",
                            relative_path="computations/trace.bin",
                        ),
                    ),
                ),
            )

        self.backend.on_launch = produce
        self.adapter.dispatch(record.attempt_id)
        artifacts = self.adapter.verify_sealed_artifacts(record.attempt_id)
        self.assertEqual(
            {(item.custody_root, item.relative_path) for item in artifacts},
            {
                ("output", "reports/result.txt"),
                ("scratch", "computations/trace.bin"),
            },
        )
        self.assertEqual(artifacts[0].path, artifacts[1].path)

    def test_terminal_provider_may_not_omit_a_custodied_file(self) -> None:
        record = self.adapter.prepare(
            self.session, self._intent("attempt-incomplete-inventory"), fence_id=self.fence.fence_id
        )
        included = self.root / "output" / record.attempt_id / "included.txt"
        omitted = self.root / "scratch" / record.attempt_id / "omitted.txt"

        def produce(_request) -> None:
            included.write_bytes(b"included")
            omitted.write_bytes(b"omitted")
            self.provider.set_observation(
                record.attempt_id,
                ProviderObservation(
                    provider_id="fake_provider",
                    provider_ref=f"fake:{record.attempt_id}",
                    state=ProviderState.SUCCEEDED,
                    detail_code="fake_succeeded",
                    artifacts=(
                        OutputArtifact(
                            name="included",
                            path=str(included),
                            sha256=sha256_file(included),
                            size_bytes=included.stat().st_size,
                            media_type="text/plain",
                            custody_root="output",
                            relative_path="included.txt",
                        ),
                    ),
                ),
            )

        self.backend.on_launch = produce
        with self.assertRaisesRegex(ContractError, "does not enumerate"):
            self.adapter.dispatch(record.attempt_id)

    def test_extra_staged_file_and_append_only_journal_are_rejected(self) -> None:
        attachment = self._attachment("context.txt", b"context")
        record = self.adapter.prepare(
            self.session,
            self._intent("attempt-extra", attachments=(attachment,)),
            fence_id=self.fence.fence_id,
        )
        root = Path(record.input_staging_root)
        os.chmod(root, stat.S_IWRITE | stat.S_IREAD | stat.S_IEXEC)
        (root / "extra.txt").write_bytes(b"extra")
        with self.assertRaisesRegex(ContractError, "missing or extra"):
            self.adapter.dispatch(record.attempt_id)
        connection = sqlite3.connect(self.journal.path)
        try:
            with self.assertRaises(sqlite3.DatabaseError):
                connection.execute(
                    "UPDATE attempts SET fence_id='different' WHERE attempt_id=?",
                    (record.attempt_id,),
                )
        finally:
            connection.close()

    def test_historical_journal_is_inert_and_never_a_launch_fallback(self) -> None:
        historical = self.root / "historical.sqlite3"
        connection = sqlite3.connect(historical)
        try:
            connection.execute(
                "CREATE TABLE journal_meta(schema_version INTEGER NOT NULL, created_at TEXT NOT NULL)"
            )
            connection.execute(
                "INSERT INTO journal_meta VALUES (6, 'historical')"
            )
            connection.commit()
        finally:
            connection.close()
        with self.assertRaisesRegex(ContractError, "inert"):
            AttemptJournal(historical)


if __name__ == "__main__":
    unittest.main()
