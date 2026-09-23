from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from research_attempt_adapter import (
    AppSelection,
    BrowserCapabilityMode,
    CodexExecProvider,
    LocalCapabilityRoot,
    McpServerSelection,
    NetworkPolicy,
    ProviderExecutionSpec,
    ProviderObservation,
    ProviderRequest,
    ProviderState,
    ResourceRequest,
    SandboxPolicy,
    SelectedCapabilities,
    StagedInputAttachment,
    VerifiedOuterContainment,
    canonical_json_bytes,
    sha256_bytes,
)
from research_attempt_adapter.codex_attempt_worker import (
    _RpcFailure,
    _TopLevelFacts,
    run_worker,
)
from research_attempt_adapter.durable_supervisor import (
    collect_complete_artifacts,
)
from research_attempt_adapter.errors import (
    ContractError,
    ProviderEffectUnknownError,
    ProviderRejectedError,
)
from research_attempt_adapter.provider import (
    LaunchRequest,
)


def _digest(character: str) -> str:
    return character * 64


class _Worker:
    def __init__(self, pid: int = 4242) -> None:
        self.pid = pid


class CodexExecProviderTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.source = self.root / "source"
        self.scratch = self.root / "scratch" / "attempt-1"
        self.output = self.root / "output" / "attempt-1"
        self.staged = self.root / "staged" / "attempt-1"
        self.state = self.root / "state"
        self.codex_home = self.root / "protected-codex-home"
        for path in (
            self.source,
            self.scratch,
            self.output,
            self.staged,
            self.codex_home,
        ):
            path.mkdir(parents=True)
        self.model_catalog = self.source / "codex-model-catalog.0.153.4.json"
        self.model_catalog.write_text(
            json.dumps(
                {
                    "models": [
                        {
                            "slug": "gpt-5.6-sol",
                            "truncation_policy": {
                                "mode": "tokens",
                                "limit": 9223372036854775807,
                            },
                        }
                    ]
                }
            ),
            encoding="utf-8",
        )
        context = self.staged / "context.txt"
        context.write_bytes(b"exact staged context")
        self.attachment = StagedInputAttachment(
            logical_name="context.txt",
            staged_path=str(context),
            sha256=sha256_bytes(context.read_bytes()),
            byte_length=context.stat().st_size,
            media_type="text/plain",
            encoding="utf-8",
        )
        manifest = self.staged / "bootstrap.manifest.json"
        manifest.write_bytes(b'{"schema":"wc.formal_context_manifest.v1"}')
        self.manifest = manifest
        self.containment = VerifiedOuterContainment(
            boundary_id="outer-1",
            boundary_digest=_digest("9"),
            public_only_egress=True,
            private_network_denied=True,
            metadata_denied=True,
            inbound_denied=True,
        )
        self.spec = ProviderExecutionSpec(
            attempt_id="attempt-1",
            session_id="session-1",
            session_digest=_digest("1"),
            fence_id="fence-1",
            provider_id="codex_exec",
            provider_profile="codex-provider.v2",
            model_profile="gpt-5.6-sol",
            reasoning_effort="ultra",
            sandbox_policy=SandboxPolicy.FORMAL_WORKSPACE,
            network_policy=NetworkPolicy.PUBLIC,
            outer_containment=self.containment,
            resource_request=ResourceRequest(vcpu=4, memory_mb=8192),
            baseline_capabilities=("shell", "web_search", "native_delegation"),
            source_working_directory=str(self.source),
            scratch_directory=str(self.scratch),
            output_directory=str(self.output),
            input_staging_root=str(self.staged),
            bootstrap_manifest_path=str(self.manifest),
            bootstrap_manifest_sha256=sha256_bytes(self.manifest.read_bytes()),
            staged_attachments=(self.attachment,),
            protected_root=str(self.codex_home),
        )
        self.bootstrap = canonical_json_bytes(
            {
                "schema": "wc.formal_attempt_bootstrap.v2",
                "attempt_id": "attempt-1",
                "session_id": "session-1",
                "manifest_path": str(self.manifest),
                "output_root": str(self.output),
                "scratch_root": str(self.scratch),
                "instruction": "Read the immutable manifest and preserve raw output.",
            }
        )
        self.launch_request = self._launch_request(self.spec)
        self.identity = {
            "platform": "test",
            "pid": 4242,
            "birth_identity": "exact-worker-1",
        }
        self.launch_calls: list[dict[str, object]] = []

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _launch_request(self, spec: ProviderExecutionSpec) -> LaunchRequest:
        return LaunchRequest(
            attempt_id="attempt-1",
            session_id="session-1",
            fence_id="fence-1",
            execution_spec=spec,
            bootstrap_bytes=self.bootstrap,
            bootstrap_sha256=sha256_bytes(self.bootstrap),
        )

    def _provider(self, **overrides) -> CodexExecProvider:
        def launch(**kwargs):
            self.launch_calls.append(kwargs)
            return _Worker()

        values = {
            "state_root": self.state,
            "model_catalog_path": self.model_catalog,
            "executable": sys.executable,
            "environment": {
                "PATH": "/usr/bin:/bin",
                "CODEX_HOME": str(self.codex_home),
                "OPENAI_API_KEY": "must-not-be-inherited",
            },
            "outer_containment": self.containment,
            "worker_launcher": launch,
            "identity_reader": lambda _pid: dict(self.identity),
            "identity_matcher": lambda value: value == self.identity,
        }
        values.update(overrides)
        return CodexExecProvider(**values)

    def _provider_request(self, spec: ProviderExecutionSpec | None = None) -> ProviderRequest:
        spec = spec or self.spec
        return ProviderRequest(
            attempt_id="attempt-1",
            session_id="session-1",
            provider_ref="codex:attempt-1",
            execution_spec_sha256=spec.digest,
            output_directory=str(self.output),
            scratch_directory=str(self.scratch),
            input_staging_root=str(self.staged),
            bootstrap_manifest_path=str(self.manifest),
            bootstrap_manifest_sha256=sha256_bytes(self.manifest.read_bytes()),
            staged_attachments=(self.attachment,),
        )

    def _write_completion(
        self, observation: ProviderObservation, spec: ProviderExecutionSpec | None = None
    ) -> None:
        spec = spec or self.spec
        launch = json.loads(
            (self.state / "attempt-1" / "launch.json").read_text(encoding="utf-8")
        )
        payload = {
            "schema": "wc.codex_attempt_completion.v3",
            "attempt_id": "attempt-1",
            "execution_spec_sha256": spec.digest,
            "launch_binding_sha256": sha256_bytes(canonical_json_bytes(launch)),
            "observation": observation.as_dict(),
        }
        (self.state / "attempt-1" / "completion.json").write_bytes(
            canonical_json_bytes(payload)
        )

    def _write_real_worker_state(self, app_server_argv: list[str]) -> Path:
        state = self.state / "attempt-1"
        state.mkdir(parents=True)
        thread_params = {
            "cwd": str(self.scratch),
            "approvalPolicy": "never",
            "model": self.spec.model_profile,
            "modelProvider": "openai",
            "allowProviderModelFallback": False,
            "config": {"agents.max_depth": 2},
            "environments": [{"environmentId": "local", "cwd": str(self.scratch)}],
            "selectedCapabilityRoots": [],
            "runtimeWorkspaceRoots": [str(self.scratch), str(self.output)],
            "permissions": "wc_formal_attempt",
            "ephemeral": True,
        }
        worker_request = {
            "schema": "wc.codex_attempt_worker_request.v1",
            "attempt_id": "attempt-1",
            "session_id": "session-1",
            "provider_ref": "codex:attempt-1",
            "execution_spec_sha256": self.spec.digest,
            "execution_spec": self.spec.as_dict(),
            "bootstrap_sha256": sha256_bytes(self.bootstrap),
            "bootstrap_base64": __import__("base64").b64encode(self.bootstrap).decode("ascii"),
            "app_server_argv": app_server_argv,
            "thread_start_params": thread_params,
            "polling_cadence_seconds": 0.01,
        }
        (state / "worker-request.json").write_bytes(canonical_json_bytes(worker_request))
        launch = {
            "schema": "wc.codex_attempt_launch.v3",
            "attempt_id": "attempt-1",
            "session_id": "session-1",
            "provider_ref": "codex:attempt-1",
            "execution_spec_sha256": self.spec.digest,
            "bootstrap_sha256": sha256_bytes(self.bootstrap),
            "bootstrap_manifest_sha256": self.spec.bootstrap_manifest_sha256,
            "staged_inventory_sha256": "not-used-by-worker",
            "worker_request_sha256": sha256_bytes(canonical_json_bytes(worker_request)),
        }
        (state / "launch.json").write_bytes(canonical_json_bytes(launch))
        return state

    @staticmethod
    def _run_real_worker(state: Path) -> subprocess.CompletedProcess[str]:
        command = [
            sys.executable,
            "-m",
            "research_attempt_adapter.codex_attempt_worker",
            "--state-directory",
            str(state),
        ]
        kwargs = {
            "args": command,
            "cwd": str(state),
            "capture_output": True,
            "text": True,
            "check": False,
        }
        if os.name == "nt":
            kwargs["creationflags"] = getattr(
                subprocess, "CREATE_NEW_PROCESS_GROUP", 0x200
            )
        else:
            kwargs["start_new_session"] = True
        return subprocess.run(**kwargs)

    def test_app_server_policy_has_no_research_or_transport_ceiling(self) -> None:
        provider = self._provider()
        argv = provider.build_argv(self.launch_request)
        joined = " ".join(argv)
        self.assertEqual(argv[1:4], ["app-server", "--stdio", "--strict-config"])
        self.assertIn('model="gpt-5.6-sol"', joined)
        self.assertIn('model_provider="openai"', joined)
        self.assertIn('model_reasoning_effort="ultra"', joined)
        self.assertIn(
            "model_catalog_json="
            + json.dumps(str(self.model_catalog.resolve()).replace("\\", "/")),
            argv,
        )
        self.assertIn('shell_environment_policy.inherit="none"', joined)
        self.assertIn(
            'shell_environment_policy.set.PATH="/usr/bin:/bin"', joined
        )
        self.assertIn('web_search="live"', joined)
        self.assertIn("features.skill_mcp_dependency_install=false", joined)
        self.assertIn("features.plugins=false", joined)
        self.assertIn("features.multi_agent_v2.enabled=false", joined)
        self.assertIn("agents.max_depth=2", joined)
        self.assertIn("agents.max_threads=9223372036854775807", joined)
        self.assertNotIn("max_concurrent_threads_per_session", joined)
        self.assertNotIn("10000", joined)
        self.assertIn(str(self.codex_home).replace("\\", "/"), joined)
        self.assertNotIn('":tmpdir" = "deny"', joined)
        self.assertNotIn('":slash_tmp" = "deny"', joined)
        self.assertNotIn(str(self.scratch / ".codex").replace("\\", "/"), joined)
        self.assertNotIn(str(self.scratch / ".agents").replace("\\", "/"), joined)
        for forbidden in (
            "timeout",
            "max_output",
            "max_prompt",
            "token_budget=true",
            "max_attempt",
            "shutdown_timeout",
        ):
            self.assertNotIn(forbidden, joined)

    def test_formal_worker_receives_only_fixed_path_and_protected_codex_home(
        self,
    ) -> None:
        observation = self._provider().launch(self.launch_request)

        self.assertEqual(observation.state, ProviderState.RUNNING)
        self.assertEqual(len(self.launch_calls), 1)
        self.assertEqual(
            self.launch_calls[0]["args"][:3],
            [sys.executable, "-m", "research_attempt_adapter.codex_attempt_worker"],
        )
        self.assertEqual(
            self.launch_calls[0]["cwd"], str(self.state / "attempt-1")
        )
        self.assertEqual(
            self.launch_calls[0]["env"],
            {
                "PATH": "/usr/bin:/bin",
                "CODEX_HOME": str(self.codex_home),
            },
        )

    def test_each_supported_model_remains_exact_in_argv_thread_and_observations(self) -> None:
        provider = self._provider()
        for model in ("gpt-5.6-sol", "gpt-6-astra"):
            with self.subTest(model=model):
                spec = replace(self.spec, model_profile=model)
                argv = provider.build_argv(self._launch_request(spec))
                self.assertIn(f'model="{model}"', argv)
                self.assertIn('model_reasoning_effort="ultra"', argv)
                self.assertIn(f'agents.default_subagent_model="{model}"', argv)
                self.assertIn('agents.default_subagent_reasoning_effort="ultra"', argv)
                params = provider._thread_start_params(spec, ())
                self.assertEqual(params["model"], model)
                self.assertEqual(params["modelProvider"], "openai")
                self.assertIs(params["allowProviderModelFallback"], False)
                self.assertEqual(params["config"]["agents.default_subagent_model"], model)
                self.assertEqual(params["config"]["agents.default_subagent_reasoning_effort"], "ultra")
                facts = _TopLevelFacts(2, model=model, reasoning_effort="ultra")
                facts.register_root_thread("root")
                facts.observe({"method": "thread/settings/updated", "params": {
                    "threadId": "root", "threadSettings": {
                        "model": model, "modelProvider": "openai", "reasoningEffort": "ultra",
                    },
                }})
                self.assertIsNone(facts.failure_detail)
                other = "gpt-6-astra" if model == "gpt-5.6-sol" else "gpt-5.6-sol"
                facts.observe({"method": "thread/settings/updated", "params": {
                    "threadId": "root", "threadSettings": {"model": other},
                }})
                self.assertEqual(facts.failure_detail, "codex_model_reroute_forbidden")
                rerouted = _TopLevelFacts(2, model=model, reasoning_effort="ultra")
                rerouted.observe({"method": "model/rerouted", "params": {}})
                self.assertEqual(rerouted.failure_detail, "codex_model_reroute_forbidden")

    def test_unknown_model_and_non_ultra_are_rejected_before_launch(self) -> None:
        for changes in ({"model_profile": "latest"}, {"model_profile": "gpt-other"},
                        {"reasoning_effort": "high"}):
            with self.subTest(changes=changes):
                with self.assertRaises(ProviderRejectedError) as rejected:
                    self._provider().launch(self._launch_request(replace(self.spec, **changes)))
                self.assertEqual(rejected.exception.code, "codex_cognitive_policy_mismatch")
        self.assertEqual(self.launch_calls, [])
        self.assertFalse((self.state / "attempt-1").exists())

    def test_worker_rejects_model_request_differing_from_bound_spec_before_process(self) -> None:
        state = self._write_real_worker_state([sys.executable, "unused.py"])
        request_path = state / "worker-request.json"
        launch_path = state / "launch.json"
        request = json.loads(request_path.read_text(encoding="utf-8"))
        request["thread_start_params"]["model"] = "gpt-6-astra"
        request_path.write_bytes(canonical_json_bytes(request))
        launch = json.loads(launch_path.read_text(encoding="utf-8"))
        launch["worker_request_sha256"] = sha256_bytes(request_path.read_bytes())
        launch_path.write_bytes(canonical_json_bytes(launch))
        worker_module = "research_attempt_adapter.codex_attempt_worker"
        identity = {"platform": "test", "pid": os.getpid(), "process_group": os.getpid()}
        with (
            patch(f"{worker_module}.process_identity", return_value=identity),
            patch(f"{worker_module}._AppServer") as app_server,
            self.assertRaisesRegex(_RpcFailure, "immutable execution specification"),
        ):
            run_worker(state)
        app_server.assert_not_called()
        self.assertFalse((state / "app-server-owner.json").exists())

    def test_model_catalog_constructor_rejects_non_absolute_or_unsafe_files(
        self,
    ) -> None:
        missing = self.source / "missing-catalog.json"
        directory = self.source / "catalog-directory"
        directory.mkdir()
        hardlink_source = self.source / "hardlink-source.json"
        hardlink_alias = self.source / "hardlink-alias.json"
        hardlink_source.write_text("{}", encoding="utf-8")
        os.link(hardlink_source, hardlink_alias)
        invalid = (
            Path("relative-catalog.json"),
            missing,
            directory,
            hardlink_alias,
        )
        for candidate in invalid:
            with self.subTest(candidate=candidate):
                with self.assertRaisesRegex(ContractError, "model_catalog_path"):
                    self._provider(model_catalog_path=candidate)

    def test_model_catalog_constructor_rejects_link(self) -> None:
        link = self.source / "linked-catalog.json"
        try:
            link.symlink_to(self.model_catalog)
        except OSError as exc:
            self.skipTest(f"symlink creation unavailable: {exc}")
        with self.assertRaisesRegex(ContractError, "model_catalog_path"):
            self._provider(model_catalog_path=link)

    def test_model_catalog_must_remain_inside_execution_source_release(self) -> None:
        outside = self.root / "outside-catalog.json"
        outside.write_text("{}", encoding="utf-8")
        provider = self._provider(model_catalog_path=outside)
        with self.assertRaisesRegex(
            ProviderRejectedError, "inside the immutable release"
        ):
            provider.build_argv(self.launch_request)

    def test_model_catalog_is_revalidated_when_execution_policy_is_checked(
        self,
    ) -> None:
        provider = self._provider()
        replacement = self.source / "replacement-catalog.json"
        replacement.write_text("{}", encoding="utf-8")
        self.model_catalog.unlink()
        os.link(replacement, self.model_catalog)
        with self.assertRaisesRegex(ProviderRejectedError, "no longer one safe"):
            provider.build_argv(self.launch_request)

    def test_exact_selected_capabilities_are_server_bound_without_flat_tool_ceiling(self) -> None:
        skill = self.source / "capabilities" / "proof-skill"
        plugin = self.source / "capabilities" / "search-plugin"
        skill.mkdir(parents=True)
        plugin.mkdir(parents=True)
        many_tools = tuple(f"tool-{index}" for index in range(100))
        selected = SelectedCapabilities(
            local_roots=(
                LocalCapabilityRoot("skill", "proof.skill", "capabilities/proof-skill"),
                LocalCapabilityRoot("plugin", "search.plugin", "capabilities/search-plugin"),
            ),
            mcp_servers=(McpServerSelection("sources", many_tools),),
            apps=(AppSelection("papers", ("lookup", "download")),),
            browser=BrowserCapabilityMode.ISOLATED_EPHEMERAL_UNAUTHENTICATED,
        )
        spec = replace(self.spec, selected_capabilities=selected)
        provider = self._provider(
            trusted_mcp_server_ids=("sources", "unselected-server"),
            trusted_app_ids=("papers", "unselected-app"),
        )
        observation = provider.launch(self._launch_request(spec))
        self.assertEqual(observation.state, ProviderState.RUNNING)
        request = json.loads(
            (self.state / "attempt-1" / "worker-request.json").read_text(
                encoding="utf-8"
            )
        )
        params = request["thread_start_params"]
        self.assertEqual(len(params["selectedCapabilityRoots"]), 2)
        config = params["config"]
        self.assertTrue(config['mcp_servers."sources".enabled'])
        self.assertEqual(
            len(config['mcp_servers."sources".enabled_tools']), len(many_tools)
        )
        self.assertFalse(config['mcp_servers."unselected-server".enabled'])
        self.assertFalse(config['apps."unselected-app".enabled'])
        self.assertTrue(config["features.browser_use_external"])
        self.assertTrue(config["features.plugins"])
        self.assertFalse(config["features.multi_agent_v2.enabled"])
        self.assertEqual(config["agents.max_depth"], 2)
        self.assertFalse(config["features.skill_mcp_dependency_install"])
        self.assertIn("provider-supplied schemas", params["developerInstructions"])
        self.assertIn("leaves cannot create descendants", params["developerInstructions"])
        serialized = canonical_json_bytes(request)
        self.assertNotIn(b"OPENAI_API_KEY", serialized)
        self.assertNotIn(b"must-not-be-inherited", serialized)

    def test_zero_extra_selections_disable_every_extra_capability(self) -> None:
        provider = self._provider(
            trusted_mcp_server_ids=("sources",), trusted_app_ids=("papers",)
        )
        provider.launch(self.launch_request)
        request = json.loads(
            (self.state / "attempt-1" / "worker-request.json").read_text(
                encoding="utf-8"
            )
        )
        params = request["thread_start_params"]
        config = params["config"]
        self.assertEqual(params["selectedCapabilityRoots"], [])
        self.assertFalse(config["orchestrator.skills.enabled"])
        self.assertFalse(config["orchestrator.mcp.enabled"])
        self.assertFalse(config["features.apps"])
        self.assertFalse(config["features.plugins"])
        self.assertFalse(config["features.browser_use"])
        self.assertFalse(config['mcp_servers."sources".enabled'])
        self.assertFalse(config['apps."papers".enabled'])

    def test_restart_observes_same_running_worker_and_never_relaunches(self) -> None:
        first = self._provider()
        first.launch(self.launch_request)
        self.assertEqual(len(self.launch_calls), 1)
        second = self._provider(
            worker_launcher=lambda **_kwargs: self.fail("restart attempted a relaunch")
        )
        observation = second.reconcile(self._provider_request())
        self.assertEqual(observation.state, ProviderState.RUNNING)
        self.assertEqual(observation.resource_facts["worker_pid"], 4242)
        replay = second.launch(self.launch_request)
        self.assertEqual(replay.state, ProviderState.RUNNING)
        self.assertEqual(len(self.launch_calls), 1)

    def test_restart_reads_atomic_terminal_record(self) -> None:
        self._provider().launch(self.launch_request)
        result_file = self.output / "nested" / "result.txt"
        result_file.parent.mkdir()
        result_file.write_text("result", encoding="utf-8")
        artifacts = collect_complete_artifacts(self.output, self.scratch)
        terminal = ProviderObservation(
            provider_id="codex_exec",
            provider_ref="codex:attempt-1",
            state=ProviderState.SUCCEEDED,
            detail_code="codex_succeeded",
            exit_code=0,
            artifacts=artifacts,
        )
        self._write_completion(terminal)
        retained_paths = [self.state / "attempt-1" / name for name in (
            "launch.json", "worker-request.json", "completion.json",
        )]
        retained_bytes = [path.read_bytes() for path in retained_paths]
        # A new release's catalog must not rewrite or redispatch the old Attempt.
        self.model_catalog.write_text('{"models":[{"slug":"gpt-6-astra"}]}', encoding="utf-8")
        restarted = self._provider(
            identity_matcher=lambda _value: self.fail(
                "terminal reconciliation must not need a live PID"
            )
        )
        observed = restarted.reconcile(self._provider_request())
        self.assertEqual(observed, terminal)
        self.assertEqual(restarted.launch(self.launch_request), terminal)
        with self.assertRaises(ProviderEffectUnknownError):
            restarted.launch(self._launch_request(replace(self.spec, model_profile="gpt-6-astra")))
        self.assertEqual(len(self.launch_calls), 1)
        self.assertEqual([path.read_bytes() for path in retained_paths], retained_bytes)

    def test_cancel_and_force_are_distinct_durable_requests_after_restart(self) -> None:
        self._provider().launch(self.launch_request)
        restarted = self._provider()
        cancelled = restarted.request_cancel(self._provider_request())
        self.assertEqual(cancelled.detail_code, "codex_cancel_requested")
        cancel_path = self.state / "attempt-1" / "cancel.request.json"
        force_path = self.state / "attempt-1" / "force_stop.request.json"
        self.assertTrue(cancel_path.is_file())
        self.assertFalse(force_path.exists())
        forced = restarted.force_stop(self._provider_request())
        self.assertEqual(forced.detail_code, "codex_force_stop_requested")
        self.assertTrue(force_path.is_file())
        self.assertNotEqual(cancel_path.read_bytes(), force_path.read_bytes())

    def test_dead_or_reused_worker_identity_is_unknown_not_attributed(self) -> None:
        self._provider().launch(self.launch_request)
        restarted = self._provider(identity_matcher=lambda _value: False)
        observation = restarted.reconcile(self._provider_request())
        self.assertEqual(observation.state, ProviderState.UNKNOWN)
        self.assertEqual(
            observation.detail_code, "codex_worker_exited_without_terminal_record"
        )

    def test_recursive_inventory_covers_output_and_scratch_without_caps(self) -> None:
        (self.output / "nested" / "deeper").mkdir(parents=True)
        (self.scratch / "proof" / "cases").mkdir(parents=True)
        (self.output / "nested" / "deeper" / "large.bin").write_bytes(
            b"x" * (8 * 1024 * 1024)
        )
        for index in range(257):
            (self.scratch / "proof" / "cases" / f"case-{index}.txt").write_text(
                str(index), encoding="utf-8"
            )
        artifacts = collect_complete_artifacts(self.output, self.scratch)
        self.assertEqual(len(artifacts), 258)
        origins = {(item.custody_root, item.relative_path) for item in artifacts}
        self.assertIn(("output", "nested/deeper/large.bin"), origins)
        self.assertIn(("scratch", "proof/cases/case-256.txt"), origins)
        text_artifact = next(
            item
            for item in artifacts
            if item.relative_path == "proof/cases/case-256.txt"
        )
        self.assertEqual(text_artifact.media_type, "text/plain")
        self.assertEqual(text_artifact.encoding, "utf-8")
        binary_artifact = next(
            item
            for item in artifacts
            if item.relative_path == "nested/deeper/large.bin"
        )
        self.assertEqual(binary_artifact.media_type, "application/octet-stream")
        self.assertIsNone(binary_artifact.encoding)
        self.assertGreater(sum(item.size_bytes for item in artifacts), 8 * 1024 * 1024)

    def test_inventory_hashes_and_checks_utf8_in_one_authoritative_stream(self) -> None:
        valid = self.output / "result.txt"
        invalid = self.scratch / "diagnostic.txt"
        valid.write_bytes("valid UTF-8\n".encode("utf-8"))
        invalid.write_bytes(b"not UTF-8: \xff\n")
        read_counts = {valid: 0, invalid: 0}
        original_open = Path.open

        def counting_open(path: Path, *args, **kwargs):
            mode = args[0] if args else kwargs.get("mode", "r")
            if path in read_counts and mode == "rb":
                read_counts[path] += 1
            return original_open(path, *args, **kwargs)

        with patch.object(Path, "open", new=counting_open):
            artifacts = collect_complete_artifacts(self.output, self.scratch)

        by_origin = {
            (item.custody_root, item.relative_path): item for item in artifacts
        }
        self.assertEqual(read_counts, {valid: 1, invalid: 1})
        self.assertEqual(by_origin[("output", "result.txt")].encoding, "utf-8")
        self.assertIsNone(by_origin[("scratch", "diagnostic.txt")].encoding)
        self.assertEqual(
            by_origin[("output", "result.txt")].sha256,
            sha256_bytes(valid.read_bytes()),
        )
        self.assertEqual(
            by_origin[("scratch", "diagnostic.txt")].sha256,
            sha256_bytes(invalid.read_bytes()),
        )

    def test_inventory_rejects_hardlinks_instead_of_silently_omitting_them(
        self,
    ) -> None:
        first = self.output / "first.bin"
        second = self.output / "second.bin"
        first.write_bytes(b"same inode")
        os.link(first, second)
        with self.assertRaises(ProviderEffectUnknownError):
            collect_complete_artifacts(self.output, self.scratch)

    def test_only_top_level_terminal_envelopes_classify_attempt_failure(self) -> None:
        facts = _TopLevelFacts(2, model="gpt-5.6-sol", reasoning_effort="ultra")
        facts.observe(
            {
                "method": "item/completed",
                "params": {
                    "item": {
                        "type": "mcpToolCall",
                        "status": "failed",
                        "error": {"message": "handled tool error"},
                        "result": {
                            "content": [{"type": "text", "text": '{"type":"error"}'}]
                        },
                    }
                },
            }
        )
        self.assertIsNone(facts.failure_detail)
        facts.observe(
            {
                "method": "turn/completed",
                "params": {"threadId": "t", "turn": {"id": "u", "status": "failed"}},
            }
        )
        self.assertEqual(facts.turn_status, "failed")
        separate = _TopLevelFacts(2, model="gpt-5.6-sol", reasoning_effort="ultra")
        separate.observe({"method": "error", "params": {"message": "provider failed"}})
        self.assertEqual(separate.failure_detail, "codex_top_level_error")

    def test_formal_delegation_preserves_direct_parentage_and_rejects_depth_three(
        self,
    ) -> None:
        facts = _TopLevelFacts(2, model="gpt-5.6-sol", reasoning_effort="ultra")
        facts.register_root_thread("formal-root")
        facts.observe(
            {
                "method": "item/completed",
                "params": {
                    "threadId": "formal-root",
                    "item": {
                        "type": "collabAgentToolCall",
                        "tool": "spawnAgent",
                        "senderThreadId": "formal-root",
                        "receiverThreadIds": ["researcher"],
                    },
                },
            }
        )
        facts.observe(
            {
                "method": "item/completed",
                "params": {
                    "threadId": "researcher",
                    "item": {
                        "type": "collabAgentToolCall",
                        "tool": "spawnAgent",
                        "senderThreadId": "researcher",
                        "receiverThreadIds": ["leaf"],
                    },
                },
            }
        )
        facts.observe(
            {
                "method": "item/completed",
                "params": {
                    "threadId": "leaf",
                    "item": {
                        "type": "agentMessage",
                        "phase": "final_answer",
                        "text": "leaf result",
                    },
                },
            }
        )
        facts.observe(
            {
                "method": "item/completed",
                "params": {
                    "threadId": "researcher",
                    "item": {
                        "type": "collabAgentToolCall",
                        "tool": "wait",
                        "senderThreadId": "researcher",
                        "receiverThreadIds": ["leaf"],
                        "agentsStates": {
                            "leaf": {"status": "completed", "message": "leaf result"}
                        },
                    },
                },
            }
        )
        self.assertEqual(
            facts.descendant_final_messages,
            [("leaf", "researcher", "leaf result")],
        )
        facts.observe(
            {
                "method": "turn/completed",
                "params": {
                    "threadId": "leaf",
                    "turn": {"id": "leaf-turn", "status": "completed"},
                },
            }
        )
        self.assertIsNone(facts.turn_status)
        facts.observe(
            {
                "method": "item/completed",
                "params": {
                    "threadId": "leaf",
                    "item": {
                        "type": "collabAgentToolCall",
                        "tool": "spawnAgent",
                        "senderThreadId": "leaf",
                        "receiverThreadIds": ["depth-three"],
                    },
                },
            }
        )
        self.assertEqual(facts.failure_detail, "codex_delegation_depth_exceeded")

    def test_formal_delegation_rejects_cross_envelope_and_malformed_parentage(
        self,
    ) -> None:
        for tool in ("spawnAgent", "sendInput", "wait"):
            with self.subTest(tool=tool):
                facts = _TopLevelFacts(2, model="gpt-5.6-sol", reasoning_effort="ultra")
                facts.register_root_thread("formal-root")
                facts.observe(
                    {
                        "method": "item/completed",
                        "params": {
                            "threadId": "formal-root",
                            "item": {
                                "type": "collabAgentToolCall",
                                "tool": tool,
                                "senderThreadId": "forged-sender",
                                "receiverThreadIds": ["child"],
                            },
                        },
                    }
                )
                self.assertEqual(
                    facts.failure_detail,
                    "codex_delegation_parentage_conflict",
                )

        for receivers in ([], ["first", "second"], ["same", "same"]):
            with self.subTest(receivers=receivers):
                malformed = _TopLevelFacts(2, model="gpt-5.6-sol", reasoning_effort="ultra")
                malformed.register_root_thread("formal-root")
                malformed.observe(
                    {
                        "method": "item/completed",
                        "params": {
                            "threadId": "formal-root",
                            "item": {
                                "type": "collabAgentToolCall",
                                "tool": "spawnAgent",
                                "senderThreadId": "formal-root",
                                "receiverThreadIds": receivers,
                            },
                        },
                    },
                )
                self.assertEqual(
                    malformed.failure_detail,
                    "codex_delegation_parentage_malformed",
                )

        self_parent = _TopLevelFacts(2, model="gpt-5.6-sol", reasoning_effort="ultra")
        self_parent.register_root_thread("formal-root")
        self_parent.observe(
            {
                "method": "item/completed",
                "params": {
                    "threadId": "formal-root",
                    "item": {
                        "type": "collabAgentToolCall",
                        "tool": "spawnAgent",
                        "senderThreadId": "formal-root",
                        "receiverThreadIds": ["formal-root"],
                    },
                },
            }
        )
        self.assertEqual(
            self_parent.failure_detail,
            "codex_delegation_parentage_conflict",
        )

        conflicting = _TopLevelFacts(2, model="gpt-5.6-sol", reasoning_effort="ultra")
        conflicting.register_root_thread("formal-root")
        conflicting.observe(
            {
                "method": "item/completed",
                "params": {
                    "threadId": "formal-root",
                    "item": {
                        "type": "collabAgentToolCall",
                        "tool": "spawnAgent",
                        "senderThreadId": "formal-root",
                        "receiverThreadIds": ["child"],
                    },
                },
            }
        )
        conflicting.observe(
            {
                "method": "thread/started",
                "params": {
                    "thread": {
                        "id": "child",
                        "parentThreadId": "different-parent",
                    }
                },
            }
        )
        self.assertEqual(
            conflicting.failure_detail,
            "codex_delegation_parentage_conflict",
        )

    def test_formal_delegation_defers_final_until_late_parentage_resolves(self) -> None:
        facts = _TopLevelFacts(2, model="gpt-5.6-sol", reasoning_effort="ultra")
        facts.register_root_thread("formal-root")
        facts.observe(
            {
                "method": "item/completed",
                "params": {
                    "threadId": "leaf",
                    "item": {
                        "type": "agentMessage",
                        "phase": "final_answer",
                        "text": "recoverable leaf result",
                    },
                },
            }
        )
        facts.observe(
            {
                "method": "thread/started",
                "params": {
                    "thread": {
                        "id": "leaf",
                        "parentThreadId": "researcher",
                    }
                },
            }
        )
        self.assertIsNone(facts.failure_detail)
        self.assertEqual(facts.descendant_final_messages, [])

        facts.observe(
            {
                "method": "item/completed",
                "params": {
                    "threadId": "formal-root",
                    "item": {
                        "type": "collabAgentToolCall",
                        "tool": "spawnAgent",
                        "senderThreadId": "formal-root",
                        "receiverThreadIds": ["researcher"],
                    },
                },
            }
        )
        facts.finalize_parentage()
        self.assertIsNone(facts.failure_detail)
        self.assertEqual(
            facts.descendant_final_messages,
            [("leaf", "researcher", "recoverable leaf result")],
        )

    def test_formal_delegation_classifies_still_unresolved_final_at_terminal(
        self,
    ) -> None:
        facts = _TopLevelFacts(2, model="gpt-5.6-sol", reasoning_effort="ultra")
        facts.register_root_thread("formal-root")
        facts.observe(
            {
                "method": "item/completed",
                "params": {
                    "threadId": "unmapped-child",
                    "item": {
                        "type": "agentMessage",
                        "phase": "final_answer",
                        "text": "result awaiting exact parentage",
                    },
                },
            }
        )
        self.assertIsNone(facts.failure_detail)
        facts.finalize_parentage()
        self.assertEqual(
            facts.failure_detail,
            "codex_delegation_parentage_unresolved",
        )

    def test_real_detached_worker_runs_fake_app_server_and_retains_nested_error(self) -> None:
        self._assert_real_worker_model("gpt-5.6-sol")

    def test_real_astra_worker_uses_bound_model_for_thread_and_turn(self) -> None:
        self._assert_real_worker_model("gpt-6-astra")

    def _assert_real_worker_model(self, model: str) -> None:
        self.spec = replace(self.spec, model_profile=model)
        fake_server = self.root / "fake_app_server.py"
        fake_server.write_text(
            """
import json
import pathlib
import sys

for line in sys.stdin:
    request = json.loads(line)
    method = request.get("method")
    if method == "initialize":
        result = {"codexHome": "protected", "platformFamily": "test", "platformOs": "test", "userAgent": "fake"}
        print(json.dumps({"jsonrpc": "2.0", "id": request["id"], "result": result}), flush=True)
    elif method == "thread/start":
        assert request["params"]["model"] == sys.argv[1]
        assert request["params"]["allowProviderModelFallback"] is False
        print(json.dumps({"jsonrpc": "2.0", "id": request["id"], "result": {"thread": {"id": "thread-1"}}}), flush=True)
    elif method == "turn/start":
        assert request["params"]["model"] == sys.argv[1]
        assert request["params"]["effort"] == "ultra"
        bootstrap = json.loads(request["params"]["input"][0]["text"])
        output = pathlib.Path(bootstrap["output_root"]) / "worker" / "result.txt"
        scratch = pathlib.Path(bootstrap["scratch_root"]) / "worker" / "trace.txt"
        output.parent.mkdir(parents=True, exist_ok=True)
        scratch.parent.mkdir(parents=True, exist_ok=True)
        output.write_text("result", encoding="utf-8")
        scratch.write_text("trace", encoding="utf-8")
        print(json.dumps({"jsonrpc": "2.0", "id": request["id"], "result": {"turn": {"id": "turn-1"}}}), flush=True)
        print(json.dumps({"method": "item/completed", "params": {"threadId": "thread-1", "turnId": "turn-1", "item": {"type": "mcpToolCall", "status": "failed", "error": {"message": "handled"}}}}), flush=True)
        print(json.dumps({"method": "item/completed", "params": {"threadId": "thread-1", "turnId": "turn-1", "item": {"type": "collabAgentToolCall", "tool": "spawnAgent", "senderThreadId": "thread-1", "receiverThreadIds": ["researcher"]}}}), flush=True)
        print(json.dumps({"method": "item/completed", "params": {"threadId": "researcher", "turnId": "researcher-turn", "item": {"type": "collabAgentToolCall", "tool": "spawnAgent", "senderThreadId": "researcher", "receiverThreadIds": ["leaf"]}}}), flush=True)
        print(json.dumps({"method": "item/completed", "params": {"threadId": "leaf", "turnId": "leaf-turn", "item": {"type": "agentMessage", "phase": "final_answer", "text": "leaf result"}}}), flush=True)
        print(json.dumps({"method": "turn/completed", "params": {"threadId": "leaf", "turn": {"id": "leaf-turn", "status": "completed"}}}), flush=True)
        print(json.dumps({"method": "item/completed", "params": {"threadId": "researcher", "turnId": "researcher-turn", "item": {"type": "collabAgentToolCall", "tool": "wait", "senderThreadId": "researcher", "receiverThreadIds": ["leaf"], "agentsStates": {"leaf": {"status": "completed", "message": "leaf result"}}}}}), flush=True)
        print(json.dumps({"method": "item/completed", "params": {"threadId": "researcher", "turnId": "researcher-turn", "item": {"type": "agentMessage", "phase": "final_answer", "text": "researcher synthesis"}}}), flush=True)
        print(json.dumps({"method": "item/completed", "params": {"threadId": "thread-1", "turnId": "turn-1", "item": {"type": "collabAgentToolCall", "tool": "wait", "senderThreadId": "thread-1", "receiverThreadIds": ["researcher"], "agentsStates": {"researcher": {"status": "completed", "message": "researcher synthesis"}}}}}), flush=True)
        print(json.dumps({"method": "item/completed", "params": {"threadId": "thread-1", "turnId": "turn-1", "item": {"type": "agentMessage", "phase": "final_answer", "text": "finished"}}}), flush=True)
        print(json.dumps({"method": "turn/completed", "params": {"threadId": "thread-1", "turn": {"id": "turn-1", "status": "completed"}}}), flush=True)
""".lstrip(),
            encoding="utf-8",
        )
        state = self._write_real_worker_state([sys.executable, str(fake_server), model])
        completed = self._run_real_worker(state)
        self.assertEqual(completed.returncode, 0, completed.stderr)
        terminal = json.loads((state / "completion.json").read_text(encoding="utf-8"))
        observation = ProviderObservation.from_dict(terminal["observation"])
        self.assertEqual(observation.state, ProviderState.SUCCEEDED)
        origins = {
            (item.custody_root, item.relative_path) for item in observation.artifacts
        }
        self.assertIn(("output", "worker/result.txt"), origins)
        self.assertIn(("scratch", "worker/trace.txt"), origins)
        self.assertIn(("output", "provider-final-message.txt"), origins)
        self.assertIn(("output", "provider-descendant-final-000001.txt"), origins)
        self.assertIn(("output", "provider-descendant-final-000002.txt"), origins)
        self.assertIn(("scratch", "app-server-events.jsonl"), origins)
        self.assertIn(("scratch", "app-server-stderr.log"), origins)
        self.assertEqual(
            (self.output / "provider-final-message.txt").read_text(encoding="utf-8"),
            "finished",
        )
        leaf = (self.output / "provider-descendant-final-000001.txt").read_text(
            encoding="utf-8"
        )
        researcher = (
            self.output / "provider-descendant-final-000002.txt"
        ).read_text(encoding="utf-8")
        self.assertIn('child_thread_id="leaf"', leaf)
        self.assertIn('parent_thread_id="researcher"', leaf)
        self.assertTrue(leaf.endswith("leaf result"))
        self.assertIn('child_thread_id="researcher"', researcher)
        self.assertIn('parent_thread_id="thread-1"', researcher)
        self.assertTrue(researcher.endswith("researcher synthesis"))

    def test_real_worker_records_top_level_rpc_error_as_atomic_failure(self) -> None:
        fake_server = self.root / "failing_app_server.py"
        fake_server.write_text(
            """
import json
import sys

for line in sys.stdin:
    request = json.loads(line)
    print(json.dumps({"jsonrpc": "2.0", "id": request["id"], "error": {"code": -32000, "message": "top-level provider failure"}}), flush=True)
""".lstrip(),
            encoding="utf-8",
        )
        state = self._write_real_worker_state([sys.executable, str(fake_server)])
        completed = self._run_real_worker(state)
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertFalse((state / "worker-error.json").exists())
        terminal = json.loads((state / "completion.json").read_text(encoding="utf-8"))
        observation = ProviderObservation.from_dict(terminal["observation"])
        self.assertEqual(observation.state, ProviderState.FAILED)
        self.assertEqual(observation.detail_code, "codex_top_level_error")
        origins = {
            (item.custody_root, item.relative_path) for item in observation.artifacts
        }
        self.assertIn(("scratch", "app-server-events.jsonl"), origins)

    def test_untrusted_connectors_and_different_containment_are_rejected(self) -> None:
        selected = SelectedCapabilities(
            mcp_servers=(McpServerSelection("untrusted", ("lookup",)),)
        )
        with self.assertRaisesRegex(ProviderRejectedError, "trusted preconfiguration"):
            self._provider().build_argv(
                self._launch_request(replace(self.spec, selected_capabilities=selected))
            )
        other = VerifiedOuterContainment(
            boundary_id="outer-2",
            boundary_digest=_digest("8"),
            public_only_egress=True,
            private_network_denied=True,
            metadata_denied=True,
            inbound_denied=True,
        )
        with self.assertRaisesRegex(ProviderRejectedError, "verified outer boundary"):
            self._provider(outer_containment=other).build_argv(self.launch_request)


if __name__ == "__main__":
    unittest.main()
