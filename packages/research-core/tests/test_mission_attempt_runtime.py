from __future__ import annotations

import hashlib
import os
import stat
import sys
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Mapping
from unittest.mock import patch


PACKAGE_ROOT = Path(__file__).resolve().parents[1]
TEST_ROOT = Path(__file__).resolve().parent
WORKSTATION_PACKAGES = (
    Path(__file__).resolve().parents[3] / "packages"
)
for value in (PACKAGE_ROOT, WORKSTATION_PACKAGES, TEST_ROOT):
    if str(value) not in sys.path:
        sys.path.insert(0, str(value))

from research_attempt_adapter.testing import FakeAttemptProvider, FakeProviderBackend, FakeSourceVerifier

from research_attempt_adapter import (  # noqa: E402
    AttemptBlockedError,
    AttemptIntent,
    AttemptJournal,
    AttemptState,
    ExecutionFence,
    NetworkPolicy,
    OutputArtifact,
    ProviderObservation,
    ProviderState,
    ResearchAttemptAdapter,
    ResultArtifact,
    ResourceRequest,
    SelectedCapabilities,
    SourceAttachment,
    SourceVerificationError,
    sha256_file,
)
from research_core.context_revision import (  # noqa: E402
    ContextRevisionRecord,
    PersistedContextRevision,
    issue_context_revision_v3,
)
from research_core.evidence_store import EvidenceCAS  # noqa: E402
from research_core import mission_attempt_runtime as runtime  # noqa: E402
from research_core.formal_session import (  # noqa: E402
    FormalRequestSelection,
    FormalSessionRevision,
    PersistedFormalSessionRevision,
    canonical_formal_session_id,
)
from research_core.json_support import canonical_json_bytes  # noqa: E402
from research_core.mission_attempt_runtime import (  # noqa: E402
    FormalAttemptBridgeError,
    FormalAttemptControl,
    FormalAttemptRuntimeFacts,
    build_formal_attempt_adapter,
    dispatch_formal_attempt,
    execute_formal_attempt_operation,
    formal_session_binding,
    installed_release_source_digest,
    prepare_formal_attempt,
    reconcile_and_settle_formal_attempt,
)
from research_core.mission_executive import (  # noqa: E402
    OwnerRevisionRef,
    prepare_direct_executive_epoch_authorized_event,
    prepare_direct_executive_epoch_bound_event,
    reissue_direct_executive_epoch_authority,
)
from research_core.research_model import deep_thaw  # noqa: E402
from research_core.workspace_paths import WorkspacePaths  # noqa: E402
from research_core.workspace_store import StaleCommandError  # noqa: E402


MISSION_ID = "mission.1"
EPOCH_ID = "epoch.formal.bridge.1"


def _digest(value: Any) -> str:
    raw = value if isinstance(value, bytes) else canonical_json_bytes(value)
    return hashlib.sha256(raw).hexdigest()


def _authority(
    mission_digest: str = "1" * 64,
    *,
    mission_revision: int = 1,
    executive_epoch_id: str = EPOCH_ID,
) -> Any:
    authorized = prepare_direct_executive_epoch_authorized_event(
        executive_epoch_id=executive_epoch_id,
        mission_root=OwnerRevisionRef(
            "mission",
            MISSION_ID,
            mission_revision,
            mission_digest,
        ),
        predecessor_checkpoint=None,
    )
    bound = prepare_direct_executive_epoch_bound_event(
        executive_epoch_id=executive_epoch_id,
        mission_id=MISSION_ID,
        goal_thread_id="thread:formal-bridge",
        workspace_root="C:/work/rh",
    )

    def readback(event: Any, ordinal: int, predecessor: Any) -> Mapping[str, Any]:
        return {
            "executive_epoch_id": event.executive_epoch_id,
            "event_ordinal": ordinal,
            "mission_id": event.mission_id,
            "project_commit_no": ordinal,
            "event_kind": event.event_kind,
            "event": deep_thaw(event.document),
            "event_digest": event.digest_sha256,
            "predecessor_event_ordinal": None if predecessor is None else 1,
            "predecessor_event_digest": (
                None if predecessor is None else predecessor.digest_sha256
            ),
            "created_actor": "bridge-test",
            "created_at": f"2026-08-24T12:00:0{ordinal}Z",
            "row_digest": str(ordinal) * 64,
        }

    return reissue_direct_executive_epoch_authority(
        event_readbacks=(
            readback(authorized, 1, None),
            readback(bound, 2, authorized),
        ),
        project_id="project.rh",
        root_identity="root.rh",
        canonical_authority_digest="c" * 64,
    )


def _open_session(authority: Any) -> PersistedFormalSessionRevision:
    mission_ref = deep_thaw(authority.mission_root)
    strategy_ref = {
        "kind": "strategy",
        "identity": "strategy.formal.1",
        "revision": 3,
        "payload_sha256": "2" * 64,
    }
    context_ref = {
        "kind": "context",
        "identity": "context.formal.1",
        "revision": 2,
        "payload_sha256": "3" * 64,
    }
    selected_bet_sha256 = "4" * 64
    session_id = canonical_formal_session_id(
        project_id=authority.project_id,
        mission_ref=mission_ref,
        strategy_ref=strategy_ref,
        selected_bet_sha256=selected_bet_sha256,
        context_ref=context_ref,
    )
    document = {
        "schema_version": 1,
        "kind": "formal_session",
        "project_id": authority.project_id,
        "mission_id": authority.mission_id,
        "session_id": session_id,
        "lifecycle": "open",
        "mission_ref": mission_ref,
        "strategy_ref": strategy_ref,
        "selected_bet_sha256": selected_bet_sha256,
        "context_ref": context_ref,
        "terminal_binding": None,
    }
    record = FormalSessionRevision(document, _digest(document))
    return PersistedFormalSessionRevision(
        record=record,
        revision=1,
        predecessor_revision=None,
        created_actor="bridge-test",
        created_at="session-revision-1",
        is_current_head=True,
    )


class _NoEvidenceStore:
    def __init__(self) -> None:
        self.evidence_writes = 0

    def __getattr__(self, name: str) -> Any:
        if "evidence" in name:
            self.evidence_writes += 1
            raise AssertionError("the formal bridge must not create Evidence")
        raise AttributeError(name)


class _ClassificationCAS:
    def __init__(self) -> None:
        self.calls: list[Mapping[str, Any]] = []

    def classify_exact_file_quarantine(self, source: Any, **kwargs: Any) -> Any:
        self.calls.append({"source": str(source), **kwargs})
        if str(kwargs["original_name"]).endswith((".jsonl", ".zip")):
            return "opaque_restricted"
        return None

    def verify_exact_file_source(
        self,
        source: Any,
        *,
        expected_sha256: str,
        expected_length: int,
    ) -> Path:
        path = Path(source)
        if (
            path.stat().st_size != expected_length
            or sha256_file(path) != expected_sha256
        ):
            raise FormalAttemptBridgeError(
                "sealed_output_changed",
                "test secure rehash found changed output",
            )
        return path


class MissionAttemptRuntimeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.working = self.root / "working"
        self.working.mkdir()
        self.authority = _authority()
        self.genesis = _open_session(self.authority)
        self.current = self.genesis
        self.store = _NoEvidenceStore()
        self.lease = object()
        self.cas = _ClassificationCAS()

        self.backend = FakeProviderBackend()
        self.provider = FakeAttemptProvider(self.backend)
        self.journal = AttemptJournal(self.root / "attempt-journal.sqlite3")
        self.adapter = ResearchAttemptAdapter(
            journal=self.journal,
            providers={self.provider.provider_id: self.provider},
            source_verifier=FakeSourceVerifier(),
            input_stage_store_root=self.root / "staged",
            scratch_store_root=self.root / "scratch",
            provider_output_root=self.root / "provider-output",
            artifact_store_root=self.root / "sealed",
            source_attachment_roots=(),
        )
        self.binding = formal_session_binding(self.genesis)
        self.fence = ExecutionFence(
            fence_id="fence.formal.bridge.1",
            provider_id=self.provider.provider_id,
            coordination_id="coord.formal.bridge.1",
            coordination_digest="5" * 64,
            authorization_id="authorization.formal.bridge.1",
            authorization_digest="6" * 64,
            source_commit="a" * 40,
            source_digest="7" * 64,
        )
        self.adapter.install_fence(self.fence)

        self.capture_preparations: list[Mapping[str, Any]] = []
        self.capture_commits: list[str] = []
        self.unique_captures: set[str] = set()
        self.terminal_calls: list[Any] = []

        for name, side_effect in (
            ("read_formal_session_revision", self._read_session),
            ("_assert_active_authority", self._assert_active_authority),
            ("prepare_raw_capture", self._prepare_capture),
            ("commit_raw_capture", self._commit_capture),
            ("terminate_formal_session", self._terminate_session),
        ):
            patcher = patch.object(runtime, name, side_effect=side_effect)
            patcher.start()
            self.addCleanup(patcher.stop)

    def _assert_active_authority(self, _store: Any, authority: Any) -> Any:
        self.assertEqual(authority.authority_sha256, self.authority.authority_sha256)
        return self.genesis.record.document["mission_ref"]

    def _read_session(
        self,
        _store: Any,
        *,
        mission_id: str,
        session_id: str,
        revision: int | None = None,
    ) -> PersistedFormalSessionRevision:
        self.assertEqual(mission_id, MISSION_ID)
        self.assertEqual(session_id, self.binding.session_id)
        return self.genesis if revision == 1 else self.current

    def _prepare_capture(self, _store: Any, **kwargs: Any) -> Any:
        self.capture_preparations.append(kwargs)
        capture_id = "raw.formal." + _digest(kwargs["observation_id"].encode())
        return SimpleNamespace(
            capture_id=capture_id,
            digest_sha256=_digest(
                {
                    "capture_id": capture_id,
                    "observation_id": kwargs["observation_id"],
                    "artifacts": [
                        {
                            "role": item.role,
                            "logical_name": item.logical_name,
                            "sha256": item.sha256,
                            "quarantine_reason": item.quarantine_reason,
                        }
                        for item in kwargs["artifacts"]
                    ],
                }
            ),
        )

    def _commit_capture(self, _store: Any, **kwargs: Any) -> Any:
        capture_id = str(kwargs["record"].capture_id)
        replayed = capture_id in self.unique_captures
        self.unique_captures.add(capture_id)
        self.capture_commits.append(capture_id)
        return SimpleNamespace(replayed=replayed)

    def _terminate_session(self, _store: Any, **kwargs: Any) -> Any:
        verified = kwargs["verified_result"]
        verified.verify_issued()
        self.terminal_calls.append(verified)
        if self.current.record.document["lifecycle"] == "terminal":
            self.assertEqual(
                self.current.record.document["terminal_binding"],
                verified.terminal_binding,
            )
            return SimpleNamespace(replayed=True)
        document = deep_thaw(self.genesis.record.document)
        document["lifecycle"] = "terminal"
        document["terminal_binding"] = deep_thaw(verified.terminal_binding)
        record = FormalSessionRevision(document, _digest(document))
        self.current = PersistedFormalSessionRevision(
            record=record,
            revision=2,
            predecessor_revision=1,
            created_actor="bridge-test",
            created_at="session-revision-2",
            is_current_head=True,
        )
        return SimpleNamespace(replayed=False)

    def _intent(
        self,
        attempt_id: str,
        *,
        previous_attempt_id: str | None = None,
        readiness: str = "8",
    ) -> AttemptIntent:
        return AttemptIntent(
            attempt_id=attempt_id,
            session_id=self.binding.session_id,
            session_digest=self.binding.session_digest,
            previous_attempt_id=previous_attempt_id,
            correction_basis=(
                None
                if previous_attempt_id is None
                else "provider readiness changed after known failure"
            ),
            readiness_state_digest=readiness * 64,
            coordination_id=self.fence.coordination_id,
            coordination_digest=self.fence.coordination_digest,
            authorization_id=self.fence.authorization_id,
            authorization_digest=self.fence.authorization_digest,
            source_commit=self.fence.source_commit,
            source_digest=self.fence.source_digest,
            provider_id=self.fence.provider_id,
            provider_profile="fake-provider.v2",
            model_profile="formal-model",
            reasoning_effort="ultra",
            resource_request=ResourceRequest(),
            baseline_capabilities=("shell",),
            network_policy=NetworkPolicy.DENIED,
            outer_containment_id=None,
            outer_containment_digest=None,
            source_attachments=(),
            working_directory=str(self.working),
        )

    def _arrange_observation(
        self,
        *,
        state: ProviderState,
        outputs: tuple[tuple[str, bytes, str, str | None], ...] = (),
    ) -> None:
        def complete(request: Any) -> None:
            artifacts = []
            output_root = Path(request.execution_spec.output_directory)
            for name, content, media_type, encoding in outputs:
                output = output_root / name
                output.write_bytes(content)
                artifacts.append(
                    OutputArtifact(
                        name=name,
                        path=str(output),
                        sha256=sha256_file(output),
                        size_bytes=output.stat().st_size,
                        media_type=media_type,
                        encoding=encoding,
                    )
                )
            self.backend.observations[request.attempt_id] = ProviderObservation(
                provider_id=self.provider.provider_id,
                provider_ref=f"fake:{request.attempt_id}",
                state=state,
                detail_code=f"fake_{state.value}",
                exit_code=0 if state is ProviderState.SUCCEEDED else 1,
                artifacts=tuple(artifacts),
            )

        self.backend.on_launch = complete

    def _prepare_and_dispatch(self, intent: AttemptIntent) -> Any:
        prepared = prepare_formal_attempt(
            self.store,
            authority=self.authority,
            adapter=self.adapter,
            session_id=self.binding.session_id,
            intent=intent,
            fence_id=self.fence.fence_id,
        )
        self.assertEqual(prepared.state, AttemptState.PREPARED)
        return dispatch_formal_attempt(
            self.store,
            authority=self.authority,
            adapter=self.adapter,
            session_id=self.binding.session_id,
            attempt_id=intent.attempt_id,
        )

    def _reconcile(self, attempt_id: str) -> Any:
        return reconcile_and_settle_formal_attempt(
            self.store,
            cas=self.cas,  # type: ignore[arg-type]
            authority=self.authority,
            adapter=self.adapter,
            session_id=self.binding.session_id,
            attempt_id=attempt_id,
            lease=self.lease,  # type: ignore[arg-type]
            actor="bridge-test",
        )

    def _controller_runtime(self) -> FormalAttemptRuntimeFacts:
        release_commit = "a" * 40
        release_root = self.root / release_commit
        codex_home = self.root / "controller-codex-home"
        release_root.mkdir(exist_ok=True)
        codex_home.mkdir(exist_ok=True)
        (release_root / ".mathematical-research-release.json").write_bytes(
            canonical_json_bytes(
                {
                    "schema_version": "wc.rh_mission_host_release.v1",
                    "release_sha": release_commit,
                    "bundle_sha256": "7" * 64,
                    "bundle_size": 4096,
                    "node_version": "v24.8.0",
                    "pnpm_version": "10.16.1",
                    "codex_version": "codex-cli 0.153.4",
                    "service_package": "@workstation-control/rh-mission-host",
                }
            )
        )
        model_catalog = (
            release_root
            / "services" / "rh-mission-host"
            / "assets"
            / "codex-model-catalog.0.153.4.json"
        )
        model_catalog.parent.mkdir(parents=True, exist_ok=True)
        model_catalog.write_text(
            '{"models":[{"slug":"gpt-5.6-sol","truncation_policy":'
            '{"mode":"tokens","limit":9223372036854775807}}]}',
            encoding="utf-8",
        )
        return FormalAttemptRuntimeFacts(
            release_root=release_root,
            release_commit=release_commit,
            state_root=self.root / "controller-state",
            codex_executable=Path(sys.executable).resolve(),
            codex_home=codex_home,
            outer_containment_id="rh-controller-boundary.1",
            trusted_mcp_server_ids=("mcp.readonly",),
            trusted_app_ids=("app.readonly",),
            polling_cadence_seconds=0.125,
        )

    def test_runtime_accepts_host_selected_executable_symlink(self) -> None:
        facts = self._controller_runtime()
        executable_link = self.root / "codex"
        try:
            executable_link.symlink_to(Path(sys.executable).resolve())
        except OSError as exc:
            self.skipTest(f"symlink creation unavailable: {exc}")

        linked_facts = replace(facts, codex_executable=executable_link)

        self.assertEqual(linked_facts.codex_executable, executable_link)

    def test_runtime_rejects_invalid_executable_paths(self) -> None:
        facts = self._controller_runtime()
        directory = self.root / "codex-directory"
        directory.mkdir()
        for label, executable in (
            ("missing", self.root / "missing-codex"),
            ("directory", directory),
        ):
            with self.subTest(label=label):
                with self.assertRaisesRegex(ValueError, "one exact existing file"):
                    replace(facts, codex_executable=executable)

        with self.assertRaisesRegex(ValueError, "Host-selected absolute path"):
            replace(facts, codex_executable=Path("relative-codex"))

    def test_runtime_rejects_broken_executable_symlink(self) -> None:
        facts = self._controller_runtime()
        broken_link = self.root / "broken-codex"
        try:
            broken_link.symlink_to(self.root / "absent-codex-target")
        except OSError as exc:
            self.skipTest(f"symlink creation unavailable: {exc}")
        with self.assertRaisesRegex(ValueError, "one exact existing file"):
            replace(facts, codex_executable=broken_link)

    def test_build_adapter_binds_fixed_pinned_catalog_inside_release(self) -> None:
        facts = self._controller_runtime()
        workspace_root = self.root / "catalog-test-workspace"
        workspace_root.mkdir()
        cas = EvidenceCAS(WorkspacePaths.from_root(workspace_root))
        cas.initialize()

        adapter = build_formal_attempt_adapter(cas=cas, runtime=facts)

        provider = adapter.providers["codex_exec"]
        self.assertEqual(
            provider.model_catalog_path,
            (
                facts.release_root
                / "services" / "rh-mission-host"
                / "assets"
                / "codex-model-catalog.0.153.4.json"
            ).resolve(),
        )

    def test_installed_release_source_identity_allows_runtime_symlinks(self) -> None:
        facts = self._controller_runtime()
        executable = facts.release_root / "node_modules" / "package" / "tool.js"
        executable.parent.mkdir(parents=True)
        executable.write_text("export default true\n", encoding="utf-8")
        bin_directory = facts.release_root / "node_modules" / ".bin"
        bin_directory.mkdir()
        tool_link = bin_directory / "tool"
        try:
            tool_link.symlink_to(Path("../package/tool.js"))
        except OSError as exc:
            self.skipTest(f"symlink creation unavailable: {exc}")

        self.assertEqual(
            installed_release_source_digest(
                facts.release_root, facts.release_commit
            ),
            "7" * 64,
        )

    def test_installed_release_source_reverification_rejects_changed_record(
        self,
    ) -> None:
        facts = self._controller_runtime()
        workspace_root = self.root / "release-change-workspace"
        workspace_root.mkdir()
        cas = EvidenceCAS(WorkspacePaths.from_root(workspace_root))
        cas.initialize()
        adapter = build_formal_attempt_adapter(cas=cas, runtime=facts)
        intent = replace(
            self._intent("attempt.formal.release-change"),
            working_directory=str(facts.release_root),
            source_commit=facts.release_commit,
            source_digest="7" * 64,
            provider_id="codex_exec",
        )
        record_path = facts.release_root / ".mathematical-research-release.json"
        record_path.write_bytes(
            record_path.read_bytes().replace(b"7" * 64, b"8" * 64)
        )

        with self.assertRaises(SourceVerificationError) as raised:
            adapter.source_verifier.verify(intent)

        self.assertEqual(raised.exception.code, "release_source_changed")

    def _controller_adapter(
        self,
        facts: FormalAttemptRuntimeFacts,
        label: str,
    ) -> tuple[ResearchAttemptAdapter, FakeProviderBackend]:
        backend = FakeProviderBackend()
        provider = FakeAttemptProvider(backend, provider_id="codex_exec")
        root = self.root / f"controller-adapter-{label}"
        return (
            ResearchAttemptAdapter(
                journal=AttemptJournal(root / "attempt-journal.sqlite3"),
                providers={provider.provider_id: provider},
                source_verifier=FakeSourceVerifier(),
                input_stage_store_root=root / "staged",
                scratch_store_root=root / "scratch",
                provider_output_root=root / "provider-output",
                artifact_store_root=root / "sealed",
                source_attachment_roots=(),
                outer_containment=facts.outer_containment,
            ),
            backend,
        )

    def _execute_controller_attempt(
        self,
        *,
        adapter: ResearchAttemptAdapter,
        facts: FormalAttemptRuntimeFacts,
        correction_basis: str | None,
    ) -> Any:
        selection = SimpleNamespace(selected_bet_sha256="4" * 64)
        with (
            patch.object(
                runtime,
                "_current_formal_request",
                return_value=(object(), object(), selection, object()),
            ),
            patch.object(
                runtime,
                "prepare_formal_session_creation",
                return_value=self.genesis,
            ),
            patch.object(runtime, "commit_formal_session_creation"),
            patch.object(
                runtime,
                "_stage_formal_context_package",
                return_value=(),
            ),
            patch.object(
                runtime,
                "_mission_policy_and_capabilities",
                return_value=("gpt-5.6-sol", "ultra", (), SelectedCapabilities()),
            ),
        ):
            return execute_formal_attempt_operation(
                self.store,
                cas=self.cas,  # type: ignore[arg-type]
                authority=self.authority,
                adapter=adapter,
                runtime=facts,
                selected_bet_sha256="4" * 64,
                correction_basis=correction_basis,
                lease=self.lease,  # type: ignore[arg-type]
                sleep=lambda _cadence: self.fail(
                    "known terminal and UNKNOWN reconciliation must not poll"
                ),
            )

    def test_scientific_context_formal_request_rejects_before_any_execution_effect(self) -> None:
        import test_formal_session as formal_fixtures

        store = formal_fixtures._FormalSessionStoreFake()
        record = issue_context_revision_v3(
            authority=store.authority,
            context_id=formal_fixtures.CONTEXT_ID,
            purpose="Keep the parent program and qualified surviving construction visible.",
            question="Can the wider construction advance the parent question?",
            treatments={
                "qualified-construction": {
                    "question": "Does the corrected construction survive?",
                    "account": "The compact-domain construction survives; its global extension is open.",
                    "qualifications": ["Compact domain only."],
                    "sources": [],
                },
            },
            exposed_treatments=(), known_omissions=(), restricted_uses=(),
            restrictions=(), independence_treatment={"method": "qualified correction"},
        )
        store.context = replace(
            store.context, record=record, payload_digest=record.digest_sha256,
        )
        store.context_history = {store.context.revision: store.context}
        store.strategy = formal_fixtures._strategy_payload(
            context_ref=formal_fixtures._context_ref(store.context),
        )
        store.strategy_history = {store.strategy.revision: store.strategy}
        selected = formal_fixtures._formal_request(store)
        facts = self._controller_runtime()
        adapter, backend = self._controller_adapter(facts, "unsupported-scientific-context")
        with (
            patch.object(runtime, "prepare_formal_session_creation", wraps=runtime.prepare_formal_session_creation) as prepare,
            patch.object(runtime, "commit_formal_session_creation", wraps=runtime.commit_formal_session_creation) as commit,
            patch.object(runtime, "_stage_formal_context_package", wraps=runtime._stage_formal_context_package) as stage,
        ):
            with self.assertRaisesRegex(StaleCommandError, "scientific Context v3 is unsupported"):
                execute_formal_attempt_operation(
                    store, cas=self.cas, authority=store.authority,
                    adapter=adapter, runtime=facts,
                    selected_bet_sha256=selected.selected_bet_sha256,
                    correction_basis=None, lease=self.lease,
                    sleep=lambda _cadence: self.fail("unsupported formal Context cannot poll"),
                )
        prepare.assert_not_called()
        commit.assert_not_called()
        stage.assert_not_called()
        self.assertEqual(store.sessions, {})
        self.assertEqual(store.commit_calls, [])
        self.assertEqual(store.raw_captures, {})
        self.assertEqual(adapter.journal.list_attempts(), ())
        self.assertEqual(backend.launch_calls, 0)
        self.assertFalse((facts.state_root / "context-packages").exists())

    def test_controller_persists_session_before_server_owned_provider_intent(
        self,
    ) -> None:
        facts = self._controller_runtime()
        source = self.root / "formal-context.json"
        source.write_bytes(b'{"exact":"context"}')
        attachment = SourceAttachment(
            logical_name="formal/context.json",
            source_path=str(source.resolve()),
            sha256=sha256_file(source),
            byte_length=source.stat().st_size,
            media_type="application/json",
            encoding="utf-8",
        )
        selection = SimpleNamespace(selected_bet_sha256="4" * 64)
        context = object()
        prepared_session = SimpleNamespace(
            record=SimpleNamespace(
                document={"session_id": self.binding.session_id}
            )
        )
        events: list[str] = []
        captured_intents: list[AttemptIntent] = []
        factual = SimpleNamespace(
            attempt_result=SimpleNamespace(attempt_state=AttemptState.SUCCEEDED)
        )

        def prepare_attempt(*_args: Any, **kwargs: Any) -> Any:
            events.append("prepare_attempt")
            captured_intents.append(kwargs["intent"])
            return SimpleNamespace(state=AttemptState.PREPARED)

        with (
            patch.object(
                runtime,
                "_current_formal_request",
                return_value=(object(), object(), selection, context),
            ),
            patch.object(
                runtime,
                "prepare_formal_session_creation",
                side_effect=lambda *_args, **_kwargs: (
                    events.append("prepare_session") or prepared_session
                ),
            ),
            patch.object(
                runtime,
                "commit_formal_session_creation",
                side_effect=lambda *_args, **_kwargs: events.append("commit_session"),
            ),
            patch.object(
                runtime,
                "_stage_formal_context_package",
                side_effect=lambda *_args, **_kwargs: (
                    events.append("stage_context") or (attachment,)
                ),
            ),
            patch.object(
                runtime,
                "_mission_policy_and_capabilities",
                return_value=(
                    "gpt-6-astra",
                    "ultra",
                    ("shell", "web_search", "native_delegation"),
                    SelectedCapabilities(),
                ),
            ),
            patch.object(runtime, "prepare_formal_attempt", side_effect=prepare_attempt),
            patch.object(
                runtime,
                "dispatch_formal_attempt",
                side_effect=lambda *_args, **_kwargs: (
                    events.append("dispatch")
                    or SimpleNamespace(state=AttemptState.RUNNING)
                ),
            ),
            patch.object(
                runtime,
                "reconcile_and_settle_formal_attempt",
                side_effect=lambda *_args, **_kwargs: (
                    events.append("reconcile") or factual
                ),
            ),
        ):
            result = execute_formal_attempt_operation(
                self.store,
                cas=self.cas,  # type: ignore[arg-type]
                authority=self.authority,
                adapter=self.adapter,
                runtime=facts,
                selected_bet_sha256="4" * 64,
                correction_basis=None,
                lease=self.lease,  # type: ignore[arg-type]
                sleep=lambda _cadence: self.fail("terminal reconciliation must not poll"),
            )

        self.assertIs(result, factual)
        self.assertLess(events.index("commit_session"), events.index("prepare_attempt"))
        self.assertEqual(
            events,
            [
                "prepare_session",
                "commit_session",
                "stage_context",
                "prepare_attempt",
                "dispatch",
                "reconcile",
            ],
        )
        intent = captured_intents[0]
        self.assertEqual(intent.session_id, self.binding.session_id)
        self.assertEqual(intent.session_digest, self.binding.session_digest)
        self.assertEqual(intent.working_directory, str(facts.release_root))
        self.assertEqual(intent.source_attachments, (attachment,))
        self.assertEqual(intent.provider_id, "codex_exec")
        self.assertEqual(intent.model_profile, "gpt-6-astra")
        self.assertEqual(intent.reasoning_effort, "ultra")
        self.assertEqual(
            intent.baseline_capabilities,
            ("shell", "web_search", "native_delegation"),
        )
        self.assertEqual(intent.selected_capabilities, SelectedCapabilities())
        self.assertEqual(
            intent.resource_request.as_dict(),
            {
                "provider_class": None,
                "vcpu": None,
                "memory_mb": None,
                "disk_mb": None,
                "provider_parameters": {},
            },
        )
        intent_keys = set(intent.as_dict())
        self.assertTrue(
            {
                "token_budget",
                "time_budget",
                "attempt_limit",
                "worker_limit",
                "tool_limit",
                "output_limit",
            }.isdisjoint(intent_keys)
        )

    def test_formal_model_comes_from_exact_mission_authority_revision(self) -> None:
        facts = self._controller_runtime()
        for model in ("gpt-5.6-sol", "gpt-6-astra"):
            with self.subTest(model=model):
                mission = deep_thaw(runtime.successor_mission_contract())
                mission["execution_policy"]["model"] = model
                digest = _digest(mission)
                authority = _authority(digest)
                read_refs = []

                def read_exact(reference):
                    read_refs.append(reference)
                    return SimpleNamespace(payload_digest=digest, payload=mission)

                store = SimpleNamespace(get_revision=read_exact)
                selected_model, effort, baseline, capabilities = runtime._mission_policy_and_capabilities(
                    store, authority=authority, runtime=facts,
                )
                self.assertEqual((selected_model, effort), (model, "ultra"))
                self.assertEqual(baseline, ("shell", "web_search", "native_delegation"))
                self.assertEqual(capabilities, SelectedCapabilities())
                self.assertEqual(len(read_refs), 1)
                self.assertEqual(read_refs[0].revision, authority.mission_root["revision"])
                with self.assertRaises(runtime.StaleCommandError):
                    runtime._mission_policy_and_capabilities(
                        store, authority=_authority("e" * 64), runtime=facts,
                    )

    def test_controller_unknown_nonsettlement_and_explicit_control_are_distinct(
        self,
    ) -> None:
        facts = self._controller_runtime()
        prepared_session = SimpleNamespace(
            record=SimpleNamespace(
                document={"session_id": self.binding.session_id}
            )
        )
        selection = SimpleNamespace(selected_bet_sha256="4" * 64)

        def run_case(
            terminal_state: AttemptState,
            control_request: str | None,
        ) -> tuple[Any, Any, Any]:
            control = FormalAttemptControl()
            if control_request == "cancel":
                control.request_cancel()
            elif control_request == "force_stop":
                control.request_force_stop()
            factual = SimpleNamespace(
                attempt_result=SimpleNamespace(attempt_state=terminal_state)
            )
            with (
                patch.object(
                    runtime,
                    "_current_formal_request",
                    return_value=(object(), object(), selection, object()),
                ),
                patch.object(
                    runtime,
                    "prepare_formal_session_creation",
                    return_value=prepared_session,
                ),
                patch.object(runtime, "commit_formal_session_creation"),
                patch.object(
                    runtime,
                    "_stage_formal_context_package",
                    return_value=(),
                ),
                patch.object(
                    runtime,
                    "_mission_policy_and_capabilities",
                    return_value=("gpt-5.6-sol", "ultra", (), SelectedCapabilities()),
                ),
                patch.object(
                    runtime,
                    "prepare_formal_attempt",
                    return_value=SimpleNamespace(
                        state=AttemptState.PREPARED,
                        attempt_id="attempt.formal.controller-control",
                    ),
                ),
                patch.object(
                    runtime,
                    "dispatch_formal_attempt",
                    return_value=SimpleNamespace(state=AttemptState.RUNNING),
                ) as dispatched,
                patch.object(
                    runtime,
                    "reconcile_and_settle_formal_attempt",
                    return_value=factual,
                ),
                patch.object(
                    self.adapter,
                    "request_cancel",
                    return_value=SimpleNamespace(
                        state=AttemptState.CANCEL_REQUESTED,
                        attempt_id="attempt.formal.controller-control",
                    ),
                ) as cancelled,
                patch.object(
                    self.adapter,
                    "force_stop",
                    return_value=SimpleNamespace(
                        state=AttemptState.FORCE_STOP_REQUESTED,
                        attempt_id="attempt.formal.controller-control",
                    ),
                ) as forced,
            ):
                result = execute_formal_attempt_operation(
                    self.store,
                    cas=self.cas,  # type: ignore[arg-type]
                    authority=self.authority,
                    adapter=self.adapter,
                    runtime=facts,
                    selected_bet_sha256="4" * 64,
                    correction_basis=None,
                    lease=self.lease,  # type: ignore[arg-type]
                    control=control,
                    sleep=lambda _cadence: self.fail(
                        "UNKNOWN and known terminal reconciliation must return"
                    ),
                )
            self.assertIs(result, factual)
            return dispatched, cancelled, forced

        dispatched, cancelled, forced = run_case(AttemptState.UNKNOWN, None)
        dispatched.assert_called_once()
        cancelled.assert_not_called()
        forced.assert_not_called()
        self.assertEqual(self.current.revision, 1)
        self.assertEqual(self.terminal_calls, [])

        dispatched, cancelled, forced = run_case(
            AttemptState.CANCELLED,
            "cancel",
        )
        dispatched.assert_not_called()
        self.assertGreaterEqual(cancelled.call_count, 1)
        forced.assert_not_called()

        dispatched, cancelled, forced = run_case(
            AttemptState.FORCE_STOPPED,
            "force_stop",
        )
        dispatched.assert_not_called()
        cancelled.assert_not_called()
        self.assertGreaterEqual(forced.call_count, 1)

    def test_controller_terminal_replay_does_not_relaunch_or_duplicate_attempt(
        self,
    ) -> None:
        facts = self._controller_runtime()
        replay_intent = replace(
            self._intent("attempt.formal.controller-replay"), model_profile="gpt-5.6-sol"
        )
        original_intent_bytes = canonical_json_bytes(replay_intent.as_dict())
        self.adapter.prepare(
            self.binding,
            replay_intent,
            fence_id=self.fence.fence_id,
        )
        document = deep_thaw(self.genesis.record.document)
        document["lifecycle"] = "terminal"
        document["terminal_binding"] = {
            "attempt_result_ref": {
                "kind": "workstation_attempt_result",
                "attempt_id": replay_intent.attempt_id,
                "session_id": self.binding.session_id,
                "attempt_state": "failed",
                "result_digest_sha256": "d" * 64,
            },
            "raw_capture_ref": None,
        }
        terminal_record = FormalSessionRevision(document, _digest(document))
        terminal = PersistedFormalSessionRevision(
            record=terminal_record,
            revision=2,
            predecessor_revision=1,
            created_actor="bridge-test",
            created_at="terminal-session-revision-2",
            is_current_head=True,
        )
        prepared_session = SimpleNamespace(
            record=SimpleNamespace(
                document={"session_id": self.binding.session_id}
            )
        )
        factual = SimpleNamespace(
            attempt_result=SimpleNamespace(attempt_state=AttemptState.FAILED)
        )

        def read_session(
            _store: Any,
            *,
            mission_id: str,
            session_id: str,
            revision: int | None = None,
        ) -> PersistedFormalSessionRevision:
            self.assertEqual(mission_id, MISSION_ID)
            self.assertEqual(session_id, self.binding.session_id)
            return self.genesis if revision == 1 else terminal

        with (
            patch.object(
                runtime,
                "_current_formal_request",
                return_value=(
                    object(),
                    object(),
                    SimpleNamespace(selected_bet_sha256="4" * 64),
                    object(),
                ),
            ),
            patch.object(
                runtime,
                "prepare_formal_session_creation",
                return_value=prepared_session,
            ),
            patch.object(runtime, "commit_formal_session_creation") as committed,
            patch.object(
                runtime,
                "read_formal_session_revision",
                side_effect=read_session,
            ),
            patch.object(
                runtime,
                "reconcile_and_settle_formal_attempt",
                return_value=factual,
            ) as reconciled,
            patch.object(runtime, "_stage_formal_context_package") as staged,
            patch.object(runtime, "prepare_formal_attempt") as prepared,
            patch.object(runtime, "dispatch_formal_attempt") as dispatched,
            patch.object(runtime, "_mission_policy_and_capabilities") as policy,
        ):
            result = execute_formal_attempt_operation(
                self.store,
                cas=self.cas,  # type: ignore[arg-type]
                authority=self.authority,
                adapter=self.adapter,
                runtime=facts,
                selected_bet_sha256="4" * 64,
                correction_basis=None,
                lease=self.lease,  # type: ignore[arg-type]
            )

        self.assertIs(result, factual)
        committed.assert_called_once()
        reconciled.assert_called_once()
        staged.assert_not_called()
        prepared.assert_not_called()
        dispatched.assert_not_called()
        policy.assert_not_called()
        self.assertEqual(len(self.adapter.journal.list_attempts()), 1)
        self.assertEqual(self.backend.launch_calls, 0)
        self.assertEqual(
            canonical_json_bytes(self.adapter.journal.get_attempt(replay_intent.attempt_id).intent.as_dict()),
            original_intent_bytes,
        )

    def test_controller_retryable_outcomes_require_explicit_corrected_successor(
        self,
    ) -> None:
        cases = (
            (ProviderState.FAILED, AttemptState.FAILED),
            (ProviderState.CANCELLED, AttemptState.CANCELLED),
            (ProviderState.FORCE_STOPPED, AttemptState.FORCE_STOPPED),
        )
        for provider_state, attempt_state in cases:
            with self.subTest(attempt_state=attempt_state.value):
                self.current = self.genesis
                self.capture_preparations.clear()
                self.capture_commits.clear()
                self.unique_captures.clear()
                self.terminal_calls.clear()
                facts = self._controller_runtime()
                adapter, backend = self._controller_adapter(
                    facts,
                    attempt_state.value,
                )
                selected_state = [provider_state]

                def complete(request: Any) -> None:
                    output = Path(request.execution_spec.output_directory) / "result.txt"
                    output.write_bytes(
                        f"raw {selected_state[0].value} output".encode("utf-8")
                    )
                    backend.observations[request.attempt_id] = ProviderObservation(
                        provider_id="codex_exec",
                        provider_ref=f"fake:{request.attempt_id}",
                        state=selected_state[0],
                        detail_code=f"fake_{selected_state[0].value}",
                        exit_code=(0 if selected_state[0] is ProviderState.SUCCEEDED else 1),
                        artifacts=(
                            OutputArtifact(
                                name="result.txt",
                                path=str(output),
                                sha256=sha256_file(output),
                                size_bytes=output.stat().st_size,
                                media_type="text/plain",
                                encoding="utf-8",
                            ),
                        ),
                    )

                backend.on_launch = complete
                first = self._execute_controller_attempt(
                    adapter=adapter,
                    facts=facts,
                    correction_basis=None,
                )

                self.assertEqual(first.attempt_result.attempt_state, attempt_state)
                self.assertFalse(first.session_is_terminal)
                self.assertIsNotNone(first.raw_capture)
                self.assertEqual(
                    self.capture_preparations[0]["artifacts"][0].role,
                    "accepted_output",
                )
                self.assertEqual(self.terminal_calls, [])
                self.assertEqual(len(adapter.journal.list_attempts()), 1)

                correction_basis = (
                    f"materially corrected readiness after {attempt_state.value}"
                )
                selected_state[0] = ProviderState.SUCCEEDED
                corrected = self._execute_controller_attempt(
                    adapter=adapter,
                    facts=facts,
                    correction_basis=correction_basis,
                )

                self.assertEqual(
                    corrected.attempt_result.attempt_state,
                    AttemptState.SUCCEEDED,
                )
                self.assertTrue(corrected.session_is_terminal)
                self.assertNotEqual(
                    corrected.attempt_result.attempt_id,
                    first.attempt_result.attempt_id,
                )
                records = adapter.journal.list_attempts()
                self.assertEqual(len(records), 2)
                first_record = adapter.journal.get_attempt(
                    first.attempt_result.attempt_id
                )
                corrected_record = adapter.journal.get_attempt(
                    corrected.attempt_result.attempt_id
                )
                self.assertEqual(first_record.session, self.binding)
                self.assertEqual(corrected_record.session, self.binding)
                self.assertEqual(
                    corrected_record.intent.previous_attempt_id,
                    first_record.attempt_id,
                )
                self.assertEqual(
                    corrected_record.intent.correction_basis,
                    correction_basis,
                )
                self.assertEqual(backend.launch_calls, 2)
                self.assertEqual(len(self.terminal_calls), 1)

    def test_controller_exact_retryable_replay_does_not_launch_successor(
        self,
    ) -> None:
        facts = self._controller_runtime()
        adapter, backend = self._controller_adapter(facts, "failed-replay")

        def fail(request: Any) -> None:
            backend.observations[request.attempt_id] = ProviderObservation(
                provider_id="codex_exec",
                provider_ref=f"fake:{request.attempt_id}",
                state=ProviderState.FAILED,
                detail_code="fake_failed",
                exit_code=1,
            )

        backend.on_launch = fail
        first = self._execute_controller_attempt(
            adapter=adapter,
            facts=facts,
            correction_basis=None,
        )
        replay = self._execute_controller_attempt(
            adapter=adapter,
            facts=facts,
            correction_basis=None,
        )

        self.assertEqual(first.attempt_result.attempt_id, replay.attempt_result.attempt_id)
        self.assertEqual(first.attempt_result.result_digest, replay.attempt_result.result_digest)
        self.assertFalse(first.session_is_terminal)
        self.assertFalse(replay.session_is_terminal)
        self.assertEqual(len(adapter.journal.list_attempts()), 1)
        self.assertEqual(backend.launch_calls, 1)
        self.assertEqual(self.terminal_calls, [])

    def test_controller_unknown_blocks_corrected_successor(self) -> None:
        facts = self._controller_runtime()
        adapter, backend = self._controller_adapter(facts, "unknown")

        def become_unknown(request: Any) -> None:
            backend.observations[request.attempt_id] = ProviderObservation(
                provider_id="codex_exec",
                provider_ref=f"fake:{request.attempt_id}",
                state=ProviderState.UNKNOWN,
                detail_code="fake_unknown",
            )

        backend.on_launch = become_unknown
        first = self._execute_controller_attempt(
            adapter=adapter,
            facts=facts,
            correction_basis=None,
        )
        self.assertEqual(first.attempt_result.attempt_state, AttemptState.UNKNOWN)
        self.assertFalse(first.session_is_terminal)

        with self.assertRaises(AttemptBlockedError) as captured:
            self._execute_controller_attempt(
                adapter=adapter,
                facts=facts,
                correction_basis="claimed correction cannot bypass unknown effect",
            )

        self.assertEqual(captured.exception.code, "retry_blocked_unknown")
        self.assertEqual(len(adapter.journal.list_attempts()), 1)
        self.assertEqual(backend.launch_calls, 1)
        self.assertEqual(self.terminal_calls, [])

    def test_context_package_preserves_exact_owner_and_file_backed_capture_bytes(
        self,
    ) -> None:
        evidence_document = {
            "kind": "evidence",
            "evidence_id": "evidence.formal.context.1",
            "mission_id": MISSION_ID,
            "factual_completion": "one exact source capture",
        }
        evidence_ref = {
            "kind": "evidence",
            "identity": "evidence.formal.context.1",
            "revision": 1,
            "payload_sha256": _digest(evidence_document),
        }
        context_document = {
            "schema_version": 1,
            "kind": "first_class_context",
            "project_id": self.authority.project_id,
            "mission_id": MISSION_ID,
            "context_id": "context.formal.package.1",
            "purpose": "Preserve the exact source basis for formal verification.",
            "question": "Does the selected construction survive exact verification?",
            "indispensable_ground": [
                {
                    "reference": evidence_ref,
                    "why": "The formal worker must inspect the exact captured source.",
                }
            ],
            "owner_source_references": [
                {
                    "reference": evidence_ref,
                    "retrieval": "Read the exact owner Evidence and Capture bytes.",
                    "provenance": "Mission-owned raw Capture custody.",
                }
            ],
            "known_omissions": [],
            "restricted_uses": [],
            "independence_treatment": {"mode": "direct_exact_source"},
            "restrictions": [],
            "invalidation_conditions": [
                {
                    "reference": evidence_ref,
                    "condition": "Invalidate if the exact Capture digest changes.",
                }
            ],
        }
        context_digest = _digest(context_document)
        context = PersistedContextRevision(
            record=ContextRevisionRecord(
                document=context_document,
                digest_sha256=context_digest,
                dependency_heads=(evidence_ref,),
            ),
            revision=1,
            payload_digest=context_digest,
            predecessor_revision=None,
            created_actor="bridge-test",
            created_at="context-revision-1",
            is_current_head=True,
        )
        context_ref = {
            "kind": "context",
            "identity": context_document["context_id"],
            "revision": 1,
            "payload_sha256": context_digest,
        }
        selected_bet = {
            "bet": "Verify one exact construction against its captured source.",
            "discriminator": "Exact formal source verification can falsify it.",
            "owner_refs": [evidence_ref],
            "formal_request": {
                "purpose": "targeted_verification",
                "context_ref": context_ref,
            },
        }
        selection = FormalRequestSelection(
            strategy_ref=self.genesis.record.document["strategy_ref"],
            selected_bet_sha256=_digest(selected_bet),
            selected_bet=selected_bet,
        )
        session_id = canonical_formal_session_id(
            project_id=self.authority.project_id,
            mission_ref=self.authority.mission_root,
            strategy_ref=selection.strategy_ref,
            selected_bet_sha256=selection.selected_bet_sha256,
            context_ref=context_ref,
        )
        session_document = {
            "schema_version": 1,
            "kind": "formal_session",
            "project_id": self.authority.project_id,
            "mission_id": MISSION_ID,
            "session_id": session_id,
            "lifecycle": "open",
            "mission_ref": deep_thaw(self.authority.mission_root),
            "strategy_ref": deep_thaw(selection.strategy_ref),
            "selected_bet_sha256": selection.selected_bet_sha256,
            "context_ref": context_ref,
            "terminal_binding": None,
        }
        session = PersistedFormalSessionRevision(
            record=FormalSessionRevision(session_document, _digest(session_document)),
            revision=1,
            predecessor_revision=None,
            created_actor="bridge-test",
            created_at="session-revision-1",
            is_current_head=True,
        )

        capture_bytes = b"exact-source:" + (b"x" * (5 * 1024 * 1024))
        capture_digest = _digest(capture_bytes)
        cas_blob = self.root / "context-cas" / capture_digest
        cas_blob.parent.mkdir()
        cas_blob.write_bytes(capture_bytes)

        class CaptureSource:
            capture_id = "raw.context.capture.1"
            artifact_ordinal = 0

            @staticmethod
            def to_payload(index: int) -> Mapping[str, Any]:
                return {
                    "source_ordinal": index,
                    "capture_id": CaptureSource.capture_id,
                    "artifact_ordinal": CaptureSource.artifact_ordinal,
                }

        capture = SimpleNamespace(
            mission_id=MISSION_ID,
            artifacts=(
                SimpleNamespace(ordinal=0, blob_sha256=capture_digest),
            ),
            to_payload=lambda: {
                "capture_id": CaptureSource.capture_id,
                "mission_id": MISSION_ID,
                "artifacts": [
                    {"ordinal": 0, "blob_sha256": capture_digest}
                ],
            },
        )
        evidence_meaning = SimpleNamespace(
            sources=(CaptureSource(),),
            interpreted_inputs=(),
        )

        class ContextCAS:
            @staticmethod
            def path_for_digest(digest: str) -> Path:
                self.assertEqual(digest, capture_digest)
                return cas_blob

            @staticmethod
            def verify_exact_file_source(
                source: Path,
                *,
                expected_sha256: str,
                expected_length: int,
            ) -> Path:
                self.assertEqual(Path(source), cas_blob)
                self.assertEqual(expected_sha256, capture_digest)
                self.assertEqual(expected_length, len(capture_bytes))
                self.assertEqual(sha256_file(source), capture_digest)
                return source

        package_root = self.root / "context-package"
        with (
            patch.object(
                runtime,
                "_verified_owner_document",
                return_value=(evidence_document, evidence_meaning),
            ),
            patch.object(runtime, "read_raw_capture", return_value=capture),
        ):
            attachments = runtime._stage_formal_context_package(
                self.store,
                cas=ContextCAS(),  # type: ignore[arg-type]
                authority=self.authority,
                selection=selection,
                context=context,
                session=session,
                package_root=package_root,
            )

        names = [item.logical_name for item in attachments]
        self.assertEqual(names, sorted(names))
        self.assertIn("formal/request.json", names)
        self.assertIn("formal/context.json", names)
        self.assertIn(
            f"owners/evidence/{evidence_ref['payload_sha256']}.json",
            names,
        )
        capture_key = hashlib.sha256(
            CaptureSource.capture_id.encode("utf-8")
        ).hexdigest()
        binary_name = f"captures/{capture_key}/0-{capture_digest}.bin"
        binary = next(item for item in attachments if item.logical_name == binary_name)
        self.assertEqual(Path(binary.source_path), cas_blob)
        self.assertEqual(binary.byte_length, len(capture_bytes))
        self.assertEqual(Path(binary.source_path).read_bytes(), capture_bytes)
        context_attachment = next(
            item
            for item in attachments
            if item.logical_name == "formal/context.json"
        )
        self.assertEqual(
            Path(context_attachment.source_path).read_bytes(),
            canonical_json_bytes(context_document),
        )
        self.assertEqual(self.store.evidence_writes, 0)

    def test_success_preserves_every_output_and_classifies_each_file(self) -> None:
        self._arrange_observation(
            state=ProviderState.SUCCEEDED,
            outputs=(
                ("result.txt", b"exact mathematical output\n", "text/plain", "utf-8"),
                (
                    "restricted.zip",
                    b"PK\x03\x04opaque output",
                    "application/zip",
                    None,
                ),
            ),
        )
        intent = self._intent("attempt.formal.success")
        dispatched = self._prepare_and_dispatch(intent)
        self.assertEqual(dispatched.state, AttemptState.SUCCEEDED)

        with (
            patch.object(
                self.adapter,
                "verify_result",
                wraps=self.adapter.verify_result,
            ) as verify_result,
            patch.object(
                self.adapter,
                "verify_sealed_artifacts",
                wraps=self.adapter.verify_sealed_artifacts,
            ) as verify_artifacts,
        ):
            outcome = self._reconcile(intent.attempt_id)

        self.assertGreaterEqual(verify_result.call_count, 1)
        verify_artifacts.assert_called_once_with(intent.attempt_id)
        self.assertTrue(outcome.session_is_terminal)
        self.assertFalse(outcome.terminal_outcome.replayed)
        self.assertEqual(len(self.capture_preparations), 1)
        capture = self.capture_preparations[0]
        self.assertEqual(capture["assignment_id"], self.binding.session_id)
        self.assertEqual(
            set(capture["provenance"]),
            {
                "kind",
                "attempt_id",
                "result_digest_sha256",
                "session_id",
                "session_digest_sha256",
            },
        )
        artifacts = capture["artifacts"]
        self.assertEqual([item.role for item in artifacts], ["accepted_output"] * 2)
        self.assertIsNone(artifacts[0].quarantine_reason)
        self.assertEqual(artifacts[1].quarantine_reason, "opaque_restricted")
        self.assertTrue(
            all(item.media_type == "application/octet-stream" for item in artifacts)
        )
        self.assertTrue(all(item.encoding is None for item in artifacts))
        self.assertEqual(len(self.cas.calls), 2)
        self.assertEqual(self.store.evidence_writes, 0)
        payload = outcome.to_payload()
        self.assertEqual(payload["attempt_state"], "succeeded")
        self.assertTrue(payload["session_is_terminal"])

    def test_capture_separates_declared_output_from_operational_evidence(self) -> None:
        def complete(request: Any) -> None:
            output_root = Path(request.execution_spec.output_directory)
            scratch_root = Path(request.execution_spec.scratch_directory)
            definitions = (
                (
                    "artifact-events",
                    output_root,
                    "app-server-events.jsonl",
                    b'{"method":"turn/completed"}\n',
                    "application/x-ndjson",
                    "utf-8",
                    "output",
                ),
                (
                    "artifact-scratch",
                    scratch_root,
                    "opaque.zip",
                    b"PK\x03\x04operational evidence",
                    "application/zip",
                    None,
                    "scratch",
                ),
                (
                    "artifact-result",
                    output_root,
                    "result.md",
                    b"# Mathematical result\n",
                    "text/markdown",
                    "utf-8",
                    "output",
                ),
            )
            artifacts = []
            for (
                name,
                root,
                relative_path,
                content,
                media_type,
                encoding,
                custody_root,
            ) in definitions:
                path = root / relative_path
                path.write_bytes(content)
                artifacts.append(
                    OutputArtifact(
                        name=name,
                        path=str(path),
                        sha256=sha256_file(path),
                        size_bytes=path.stat().st_size,
                        media_type=media_type,
                        encoding=encoding,
                        custody_root=custody_root,
                        relative_path=relative_path,
                    )
                )
            self.backend.observations[request.attempt_id] = ProviderObservation(
                provider_id=self.provider.provider_id,
                provider_ref=f"fake:{request.attempt_id}",
                state=ProviderState.SUCCEEDED,
                detail_code="fake_succeeded",
                exit_code=0,
                artifacts=tuple(artifacts),
            )

        self.backend.on_launch = complete
        intent = self._intent("attempt.formal.channel-separation")
        self._prepare_and_dispatch(intent)

        outcome = self._reconcile(intent.attempt_id)

        self.assertTrue(outcome.session_is_terminal)
        artifacts = self.capture_preparations[0]["artifacts"]
        self.assertEqual(
            [(item.role, item.logical_name) for item in artifacts],
            [
                ("accepted_output", "output/result.md"),
                ("evidence_only", "output/app-server-events.jsonl"),
                ("evidence_only", "scratch/opaque.zip"),
            ],
        )
        self.assertTrue(
            all(item.media_type == "application/octet-stream" for item in artifacts)
        )
        self.assertTrue(all(item.encoding is None for item in artifacts))
        self.assertEqual(
            [
                (item.artifact.media_type, item.artifact.encoding)
                for item in outcome.attempt_result.artifacts
            ],
            [
                ("application/x-ndjson", "utf-8"),
                ("application/zip", None),
                ("text/markdown", "utf-8"),
            ],
        )
        self.assertIsNone(artifacts[0].quarantine_reason)
        self.assertEqual(artifacts[1].quarantine_reason, "opaque_restricted")
        self.assertEqual(artifacts[2].quarantine_reason, "opaque_restricted")
        self.assertEqual(
            [call["original_name"] for call in self.cas.calls],
            [
                "output/result.md",
                "output/app-server-events.jsonl",
                "scratch/opaque.zip",
            ],
        )

    def test_capture_preserves_identical_output_and_scratch_origins(self) -> None:
        def complete(request: Any) -> None:
            output_root = Path(request.execution_spec.output_directory)
            scratch_root = Path(request.execution_spec.scratch_directory)
            payload = b"same exact bytes\n"
            scratch = scratch_root / "trace.txt"
            output = output_root / "result.txt"
            scratch.write_bytes(payload)
            output.write_bytes(payload)
            self.backend.observations[request.attempt_id] = ProviderObservation(
                provider_id=self.provider.provider_id,
                provider_ref=f"fake:{request.attempt_id}",
                state=ProviderState.SUCCEEDED,
                detail_code="fake_succeeded",
                exit_code=0,
                artifacts=(
                    OutputArtifact(
                        name="scratch-trace",
                        path=str(scratch),
                        sha256=sha256_file(scratch),
                        size_bytes=scratch.stat().st_size,
                        media_type="text/plain",
                        encoding="utf-8",
                        custody_root="scratch",
                        relative_path="trace.txt",
                    ),
                    OutputArtifact(
                        name="mathematical-result",
                        path=str(output),
                        sha256=sha256_file(output),
                        size_bytes=output.stat().st_size,
                        media_type="text/plain",
                        encoding="utf-8",
                        custody_root="output",
                        relative_path="result.txt",
                    ),
                ),
            )

        self.backend.on_launch = complete
        intent = self._intent("attempt.formal.identical-origins")
        self._prepare_and_dispatch(intent)

        outcome = self._reconcile(intent.attempt_id)

        self.assertTrue(outcome.session_is_terminal)
        artifacts = self.capture_preparations[0]["artifacts"]
        self.assertEqual(
            [(item.role, item.logical_name) for item in artifacts],
            [
                ("accepted_output", "output/result.txt"),
                ("evidence_only", "scratch/trace.txt"),
            ],
        )
        self.assertEqual(artifacts[0].sha256, artifacts[1].sha256)
        self.assertEqual(artifacts[0].source_path, artifacts[1].source_path)

    def test_result_artifact_deduplication_is_scoped_to_exact_origin(self) -> None:
        sealed = self.root / "sealed-identical"
        sealed.write_bytes(b"same exact bytes\n")
        artifact = OutputArtifact(
            name="mathematical-result",
            path=str(sealed),
            sha256=sha256_file(sealed),
            size_bytes=sealed.stat().st_size,
            media_type="text/plain",
            encoding="utf-8",
            custody_root="output",
            relative_path="result.txt",
        )

        unique = runtime._unique_result_artifacts(
            (
                ResultArtifact(event_sequence=3, evidence_only=True, artifact=artifact),
                ResultArtifact(
                    event_sequence=4, evidence_only=False, artifact=artifact
                ),
            ),
            accept_trusted=True,
        )

        self.assertEqual(unique, ((artifact, True),))

    def test_known_failure_without_output_preserves_open_session_for_correction(
        self,
    ) -> None:
        self._arrange_observation(state=ProviderState.FAILED)
        intent = self._intent("attempt.formal.failed")
        self.assertEqual(self._prepare_and_dispatch(intent).state, AttemptState.FAILED)

        outcome = self._reconcile(intent.attempt_id)

        self.assertFalse(outcome.session_is_terminal)
        self.assertIsNone(outcome.raw_capture)
        self.assertEqual(self.capture_preparations, [])
        self.assertEqual(self.terminal_calls, [])
        self.assertEqual(self.current.revision, 1)
        self.assertEqual(self.store.evidence_writes, 0)

    def test_historical_failed_terminal_fact_replays_without_reopening(self) -> None:
        self._arrange_observation(state=ProviderState.FAILED)
        intent = self._intent("attempt.formal.historical-failed")
        self._prepare_and_dispatch(intent)
        result = self.adapter.verify_result(intent.attempt_id)
        document = deep_thaw(self.genesis.record.document)
        document["lifecycle"] = "terminal"
        document["terminal_binding"] = {
            "attempt_result_ref": {
                "kind": "workstation_attempt_result",
                "attempt_id": result.attempt_id,
                "session_id": result.session_id,
                "attempt_state": result.attempt_state.value,
                "result_digest_sha256": result.result_digest,
            },
            "raw_capture_ref": None,
        }
        record = FormalSessionRevision(document, _digest(document))
        self.current = PersistedFormalSessionRevision(
            record=record,
            revision=2,
            predecessor_revision=1,
            created_actor="bridge-test",
            created_at="historical-terminal-session-revision-2",
            is_current_head=True,
        )

        replay = self._reconcile(intent.attempt_id)

        self.assertTrue(replay.session_was_terminal)
        self.assertTrue(replay.session_is_terminal)
        self.assertTrue(replay.terminal_outcome.replayed)
        self.assertEqual(self.current.record.document["terminal_binding"], document["terminal_binding"])
        self.assertEqual(len(self.terminal_calls), 1)

    def test_unknown_reconciles_without_session_settlement(self) -> None:
        self._arrange_observation(state=ProviderState.UNKNOWN)
        intent = self._intent("attempt.formal.unknown")
        self.assertEqual(self._prepare_and_dispatch(intent).state, AttemptState.UNKNOWN)

        outcome = self._reconcile(intent.attempt_id)

        self.assertEqual(outcome.attempt_result.attempt_state, AttemptState.UNKNOWN)
        self.assertFalse(outcome.session_is_terminal)
        self.assertIsNone(outcome.terminal_outcome)
        self.assertEqual(self.terminal_calls, [])
        self.assertEqual(self.current.revision, 1)

    def test_fenced_attempt_closes_session_without_promoting_late_output(self) -> None:
        intent = self._intent("attempt.formal.evidence-only")
        prepared = prepare_formal_attempt(
            self.store,
            authority=self.authority,
            adapter=self.adapter,
            session_id=self.binding.session_id,
            intent=intent,
            fence_id=self.fence.fence_id,
        )
        self.assertEqual(prepared.state, AttemptState.PREPARED)
        self.adapter.revoke_fence(self.fence.fence_id)
        output = self.root / "provider-output" / intent.attempt_id / "late.txt"
        output.write_bytes(b"late output")
        self.adapter.ingest_observation(
            intent.attempt_id,
            ProviderObservation(
                provider_id=self.provider.provider_id,
                provider_ref=f"fake:{intent.attempt_id}",
                state=ProviderState.SUCCEEDED,
                detail_code="late_succeeded",
                exit_code=0,
                artifacts=(
                    OutputArtifact(
                        name="late.txt",
                        path=str(output),
                        sha256=sha256_file(output),
                        size_bytes=output.stat().st_size,
                        media_type="text/plain",
                        encoding="utf-8",
                    ),
                ),
            ),
        )

        outcome = self._reconcile(intent.attempt_id)

        self.assertEqual(outcome.attempt_result.attempt_state, AttemptState.FENCED)
        self.assertTrue(outcome.session_is_terminal)
        self.assertEqual(
            self.capture_preparations[0]["artifacts"][0].role, "evidence_only"
        )
        self.assertEqual(len(self.terminal_calls), 1)
        terminal = self.terminal_calls[0].terminal_binding
        self.assertEqual(terminal["attempt_result_ref"]["attempt_state"], "fenced")
        self.assertEqual(
            terminal["raw_capture_ref"]["capture_id"],
            outcome.raw_capture.capture_id,
        )

    def test_later_mission_authority_cannot_prepare_or_dispatch_old_session(self) -> None:
        intent = self._intent("attempt.formal.before-reauthorization")
        prepared = prepare_formal_attempt(
            self.store,
            authority=self.authority,
            adapter=self.adapter,
            session_id=self.binding.session_id,
            intent=intent,
            fence_id=self.fence.fence_id,
        )
        self.assertEqual(prepared.state, AttemptState.PREPARED)
        attempts = self.journal.list_attempts()
        events = self.journal.list_events(intent.attempt_id)
        genesis_bytes = canonical_json_bytes(self.genesis.record.document)
        fence = self.journal.get_fence(self.fence.fence_id)
        later_authority = _authority(
            "e" * 64,
            mission_revision=3,
            executive_epoch_id="epoch.formal.after-reauthorization",
        )
        self.assertNotEqual(later_authority.mission_root, self.authority.mission_root)
        with patch.object(
            runtime, "_assert_active_authority", return_value=later_authority.mission_root,
        ):
            with self.assertRaises(FormalAttemptBridgeError) as prepare_error:
                prepare_formal_attempt(
                    self.store,
                    authority=later_authority,
                    adapter=self.adapter,
                    session_id=self.binding.session_id,
                    intent=self._intent("attempt.formal.silent-session-adoption"),
                    fence_id=self.fence.fence_id,
                )
            self.assertEqual(prepare_error.exception.code, "formal_session_mission_root_stale")
            with self.assertRaises(FormalAttemptBridgeError) as dispatch_error:
                dispatch_formal_attempt(
                    self.store,
                    authority=later_authority,
                    adapter=self.adapter,
                    session_id=self.binding.session_id,
                    attempt_id=intent.attempt_id,
                )
            self.assertEqual(dispatch_error.exception.code, "formal_session_mission_root_stale")
        self.assertEqual(self.journal.list_attempts(), attempts)
        self.assertEqual(self.journal.list_events(intent.attempt_id), events)
        self.assertEqual(self.journal.get_fence(self.fence.fence_id), fence)
        self.assertEqual(canonical_json_bytes(self.genesis.record.document), genesis_bytes)
        self.assertIs(self.current, self.genesis)
        self.assertEqual(self.backend.launch_calls, 0)
        self.assertEqual(self.backend.reconcile_calls, 0)
        self.assertEqual(self.capture_preparations, [])
        self.assertEqual(self.terminal_calls, [])

    def test_attempt_intent_binding_mismatch_is_rejected_before_journal_write(
        self,
    ) -> None:
        intent = replace(
            self._intent("attempt.formal.wrong-binding"),
            session_digest="f" * 64,
        )
        with self.assertRaisesRegex(
            FormalAttemptBridgeError,
            "exact bridge-constructed Session",
        ):
            prepare_formal_attempt(
                self.store,
                authority=self.authority,
                adapter=self.adapter,
                session_id=self.binding.session_id,
                intent=intent,
                fence_id=self.fence.fence_id,
            )
        self.assertEqual(self.journal.list_attempts(), ())

    def test_verified_result_binding_mismatch_cannot_capture_or_settle(self) -> None:
        self._arrange_observation(state=ProviderState.FAILED)
        intent = self._intent("attempt.formal.wrong-result")
        self._prepare_and_dispatch(intent)
        original = self.adapter.verify_result

        def wrong_result(attempt_id: str) -> Any:
            return replace(original(attempt_id), session_digest="f" * 64)

        with (
            patch.object(self.adapter, "verify_result", side_effect=wrong_result),
            self.assertRaisesRegex(
                FormalAttemptBridgeError,
                "original Session binding",
            ),
        ):
            self._reconcile(intent.attempt_id)
        self.assertEqual(self.capture_preparations, [])
        self.assertEqual(self.terminal_calls, [])

    def test_lost_reply_replay_duplicates_neither_capture_nor_terminal_revision(
        self,
    ) -> None:
        self._arrange_observation(
            state=ProviderState.SUCCEEDED,
            outputs=(("result.txt", b"stable result", "text/plain", "utf-8"),),
        )
        intent = self._intent("attempt.formal.replay")
        self._prepare_and_dispatch(intent)

        first = self._reconcile(intent.attempt_id)
        second = self._reconcile(intent.attempt_id)

        self.assertFalse(first.terminal_outcome.replayed)
        self.assertTrue(second.terminal_outcome.replayed)
        self.assertEqual(len(self.unique_captures), 1)
        self.assertEqual(len(self.capture_commits), 2)
        self.assertEqual(self.current.revision, 2)
        self.assertEqual(len(self.terminal_calls), 2)
        self.assertEqual(
            first.attempt_result.result_digest,
            second.attempt_result.result_digest,
        )

    def test_late_output_is_captured_as_evidence_only_without_rebinding_terminal(
        self,
    ) -> None:
        self._arrange_observation(
            state=ProviderState.SUCCEEDED,
            outputs=(("result.txt", b"settled result", "text/plain", "utf-8"),),
        )
        intent = self._intent("attempt.formal.late-output")
        self._prepare_and_dispatch(intent)
        first = self._reconcile(intent.attempt_id)
        first_binding = deep_thaw(self.current.record.document["terminal_binding"])
        self.assertTrue(first.session_is_terminal)

        original = (
            self.root
            / "provider-output"
            / intent.attempt_id
            / "result.txt"
        )
        late = self.root / "provider-output" / intent.attempt_id / "late.txt"
        late.write_bytes(b"late observation")
        self.adapter.ingest_observation(
            intent.attempt_id,
            ProviderObservation(
                provider_id=self.provider.provider_id,
                provider_ref=f"fake:{intent.attempt_id}",
                state=ProviderState.SUCCEEDED,
                detail_code="late_succeeded",
                exit_code=0,
                artifacts=(
                    OutputArtifact(
                        name="result.txt",
                        path=str(original),
                        sha256=sha256_file(original),
                        size_bytes=original.stat().st_size,
                        media_type="text/plain",
                        encoding="utf-8",
                    ),
                    OutputArtifact(
                        name="late.txt",
                        path=str(late),
                        sha256=sha256_file(late),
                        size_bytes=late.stat().st_size,
                        media_type="text/plain",
                        encoding="utf-8",
                    ),
                ),
            ),
        )

        late_result = self._reconcile(intent.attempt_id)

        self.assertTrue(late_result.session_is_terminal)
        self.assertIsNone(late_result.terminal_outcome)
        self.assertFalse(late_result.to_payload()["result_bound_to_session"])
        self.assertEqual(len(self.terminal_calls), 1)
        self.assertEqual(
            deep_thaw(self.current.record.document["terminal_binding"]),
            first_binding,
        )
        self.assertTrue(
            all(
                artifact.role == "evidence_only"
                for artifact in self.capture_preparations[-1]["artifacts"]
            )
        )

    def test_fresh_rehash_detects_change_after_both_workstation_verifications(
        self,
    ) -> None:
        self._arrange_observation(
            state=ProviderState.SUCCEEDED,
            outputs=(("result.txt", b"stable result", "text/plain", "utf-8"),),
        )
        intent = self._intent("attempt.formal.rehash")
        self._prepare_and_dispatch(intent)
        original = self.adapter.verify_sealed_artifacts

        def verify_then_change(attempt_id: str) -> Any:
            artifacts = original(attempt_id)
            path = Path(artifacts[0].path)
            os.chmod(path, stat.S_IREAD | stat.S_IWRITE)
            path.write_bytes(b"mutated bytes")
            return artifacts

        with (
            patch.object(
                self.adapter,
                "verify_sealed_artifacts",
                side_effect=verify_then_change,
            ),
            self.assertRaises(FormalAttemptBridgeError) as captured,
        ):
            self._reconcile(intent.attempt_id)
        self.assertEqual(captured.exception.code, "sealed_output_changed")
        self.assertEqual(self.capture_preparations, [])
        self.assertEqual(self.terminal_calls, [])

    def test_explicit_corrected_retry_uses_same_immutable_session(self) -> None:
        self._arrange_observation(state=ProviderState.FAILED)
        first = self._intent("attempt.formal.retry.1", readiness="8")
        self.assertEqual(self._prepare_and_dispatch(first).state, AttemptState.FAILED)
        corrected = self._intent(
            "attempt.formal.retry.2",
            previous_attempt_id=first.attempt_id,
            readiness="9",
        )

        prepared = prepare_formal_attempt(
            self.store,
            authority=self.authority,
            adapter=self.adapter,
            session_id=self.binding.session_id,
            intent=corrected,
            fence_id=self.fence.fence_id,
        )

        self.assertEqual(prepared.state, AttemptState.PREPARED)
        self.assertEqual(prepared.session, self.binding)
        self.assertEqual(prepared.intent.previous_attempt_id, first.attempt_id)


if __name__ == "__main__":
    unittest.main()
