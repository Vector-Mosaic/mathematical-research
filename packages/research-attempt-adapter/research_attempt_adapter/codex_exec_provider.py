"""Durable noninteractive Codex provider for one immutable formal Attempt.

The public provider remains the single Workstation owner.  Internally it starts
one detached Attempt worker which owns one Codex App Server process, records its
own terminal observation atomically, and remains observable after this Python
provider object or process is replaced.
"""

from __future__ import annotations

import base64
import json
import math
import os
import shutil
import stat
import subprocess
import sys
from pathlib import Path
from typing import Callable, Mapping, Sequence

from .durable_supervisor import (
    identity_matches,
    process_identity,
    read_json_object,
    require_no_link_components,
    write_json_once,
)
from .errors import ContractError, ProviderEffectUnknownError, ProviderRejectedError
from .models import (
    BrowserCapabilityMode,
    LocalCapabilityKind,
    NetworkPolicy,
    ProviderExecutionSpec,
    ProviderObservation,
    ProviderState,
    VerifiedOuterContainment,
    canonical_json_bytes,
    require_capability_identifier,
    sha256_bytes,
    sha256_file,
    staged_attachment_path,
    staged_inventory_digest,
)
from .provider import LaunchRequest, ProviderRequest


_ENV_ALLOWLIST = {
    "APPDATA",
    "CODEX_HOME",
    "COMSPEC",
    "HOME",
    "LANG",
    "LC_ALL",
    "LOCALAPPDATA",
    "PATH",
    "PATHEXT",
    "SYSTEMROOT",
    "TEMP",
    "TERM",
    "TMP",
    "USERPROFILE",
    "WINDIR",
}
_CODEX_PROVIDER_PROFILE = "codex-provider.v2"
_CODEX_MODEL_PROVIDER = "openai"
_CODEX_MODELS = frozenset({"gpt-5.6-sol", "gpt-6-astra"})
_CODEX_REASONING_EFFORT = "ultra"
_BASELINE_CAPABILITIES = frozenset({"shell", "web_search", "native_delegation"})
_PERMISSION_ID = "wc_formal_attempt"
_LOCAL_ENVIRONMENT_ID = "local"
# Pinned Codex 0.153.4 has no null/unbounded V1 thread-capacity representation. Its
# signed-integer maximum removes the product default without preallocating jobs;
# actual provider and machine capacity remain observed facts. Depth remains a
# structural parentage boundary rather than a worker-count quota.
_UNBOUNDED_V1_THREAD_CAPACITY = "9223372036854775807"
_FORMAL_DELEGATION_MAX_DEPTH = 2


def _require_execution_spec_cognitive_policy(spec: ProviderExecutionSpec) -> None:
    if (
        spec.provider_id != "codex_exec"
        or spec.provider_profile != _CODEX_PROVIDER_PROFILE
        or spec.model_profile not in _CODEX_MODELS
        or spec.reasoning_effort != _CODEX_REASONING_EFFORT
    ):
        raise ProviderRejectedError(
            "codex_cognitive_policy_mismatch",
            "Codex requires the exact bound Sol or Astra model with ultra and no fallback.",
        )


class CodexExecProvider:
    """One durable Attempt-owned worker job; recorded effects are never relaunched."""

    provider_id = "codex_exec"

    def __init__(
        self,
        *,
        state_root: str | Path,
        model_catalog_path: str | Path,
        executable: str = "codex",
        environment: Mapping[str, str] | None = None,
        outer_containment: VerifiedOuterContainment | None = None,
        trusted_mcp_server_ids: Sequence[str] = (),
        trusted_app_ids: Sequence[str] = (),
        polling_cadence_seconds: float = 0.25,
        worker_launcher=None,
        identity_reader: Callable[[int], dict[str, object] | None] = process_identity,
        identity_matcher: Callable[[object], bool] = identity_matches,
    ) -> None:
        self.executable = self._resolve_executable(executable)
        self.model_catalog_path = self._require_model_catalog_path(model_catalog_path)
        base_environment = dict(os.environ if environment is None else environment)
        self._environment = {
            key: value
            for key, value in base_environment.items()
            if key.upper() in _ENV_ALLOWLIST
        }
        if (
            outer_containment is not None
            and type(outer_containment) is not VerifiedOuterContainment
        ):
            raise ContractError(
                "invalid_outer_containment",
                "Provider containment must be an independently verified fact.",
            )
        self._outer_containment = outer_containment
        self._trusted_mcp_server_ids = self._trusted_ids(
            trusted_mcp_server_ids, "trusted_mcp_server_ids"
        )
        self._trusted_app_ids = self._trusted_ids(
            trusted_app_ids, "trusted_app_ids"
        )
        if (
            isinstance(polling_cadence_seconds, bool)
            or not isinstance(polling_cadence_seconds, (int, float))
            or not math.isfinite(float(polling_cadence_seconds))
            or polling_cadence_seconds <= 0
        ):
            raise ContractError(
                "invalid_polling_cadence",
                "polling_cadence_seconds must be a positive transport cadence.",
            )
        self.polling_cadence_seconds = float(polling_cadence_seconds)
        self.state_root = Path(state_root).absolute()
        self.state_root.mkdir(parents=True, exist_ok=True)
        self._require_directory(self.state_root, "provider_state_root")
        self._worker_launcher = worker_launcher or subprocess.Popen
        self._identity_reader = identity_reader
        self._identity_matcher = identity_matcher

    def build_argv(self, request: LaunchRequest) -> list[str]:
        """Return the dedicated App Server invocation used by the durable worker."""

        spec = request.execution_spec
        selected_roots = self._require_execution_spec_policy(spec)
        writable = [spec.scratch_directory, spec.output_directory]
        readable = [spec.source_working_directory, spec.input_staging_root, *selected_roots]
        workspace_roots = ", ".join(
            f"{json.dumps(self._forward(path))} = true" for path in [*writable, *readable]
        )
        filesystem_parts = [
            '":root" = "deny"',
            '":minimal" = "read"',
            '":workspace_roots" = "read"',
            *(f"{json.dumps(self._forward(path))} = \"read\"" for path in readable),
            *(f"{json.dumps(self._forward(path))} = \"write\"" for path in writable),
        ]
        if spec.protected_root is not None:
            filesystem_parts.append(
                f"{json.dumps(self._forward(spec.protected_root))} = \"deny\""
            )
        profile = ", ".join(
            (
                '{ description = "Formal Attempt confinement"',
                'extends = ":workspace"',
                f"workspace_roots = {{ {workspace_roots} }}",
                f"filesystem = {{ {', '.join(filesystem_parts)} }}",
                "network = { enabled = "
                + ("true" if spec.network_policy is NetworkPolicy.PUBLIC else "false")
                + " } }",
            )
        )
        overrides: list[str] = [
            f"permissions.{_PERMISSION_ID}={profile}",
            f"default_permissions={json.dumps(_PERMISSION_ID)}",
            f"model={json.dumps(spec.model_profile)}",
            f"model_provider={json.dumps(_CODEX_MODEL_PROVIDER)}",
            f"model_reasoning_effort={json.dumps(spec.reasoning_effort)}",
            (
                "model_catalog_json="
                + json.dumps(self._forward(self.model_catalog_path))
            ),
            (
                'web_search="live"'
                if "web_search" in spec.baseline_capabilities
                else 'web_search="disabled"'
            ),
            "apps._default.enabled=false",
            "apps._default.destructive_enabled=false",
            "apps._default.open_world_enabled=false",
            'shell_environment_policy.inherit="none"',
            'shell_environment_policy.set.PATH="/usr/bin:/bin"',
            "shell_environment_policy.ignore_default_excludes=false",
            "project_root_markers=[]",
            "skills.bundled.enabled=false",
            "orchestrator.skills.enabled=true",
            "orchestrator.mcp.enabled=false",
            "tools.experimental_request_user_input.enabled=false",
            "features.browser_use_full_cdp_access=false",
            "features.goals=false",
            "features.in_app_browser=false",
            "features.multi_agent=true",
            "features.multi_agent_v2.enabled=false",
            f"agents.default_subagent_model={json.dumps(spec.model_profile)}",
            f"agents.default_subagent_reasoning_effort={json.dumps(spec.reasoning_effort)}",
            f"agents.max_depth={_FORMAL_DELEGATION_MAX_DEPTH}",
            "agents.max_threads=" + _UNBOUNDED_V1_THREAD_CAPACITY,
            "features.plugin_sharing=false",
            "features.plugins=false",
            "features.remote_plugin=false",
            "features.remote_models=false",
            "features.rollout_budget=false",
            "features.shell_tool=true",
            "features.skill_mcp_dependency_install=false",
            "features.token_budget=false",
        ]
        return [
            self.executable,
            "app-server",
            "--stdio",
            "--strict-config",
            *[item for override in overrides for item in ("--config", override)],
        ]

    def launch(self, request: LaunchRequest) -> ProviderObservation:
        spec = request.execution_spec
        selected_roots = self._require_execution_spec_policy(spec)
        self._validate_bootstrap(request)
        self._verify_staged_inputs(request)
        state_directory = self._state_directory(request.attempt_id)
        state_directory.mkdir(mode=0o700, exist_ok=True)
        self._require_directory(state_directory, "attempt_provider_state")
        completion_path = self._completion_path(request.attempt_id)
        if completion_path.exists():
            return self._read_completion(
                completion_path, request.attempt_id, spec.digest
            )
        launch_path = self._launch_path(request.attempt_id)
        if launch_path.exists():
            launch = read_json_object(launch_path)
            self._validate_launch_binding(launch, request)
            return self._observe_recorded_launch(request.attempt_id, launch)

        worker_request = {
            "schema": "wc.codex_attempt_worker_request.v1",
            "attempt_id": request.attempt_id,
            "session_id": request.session_id,
            "provider_ref": self._provider_ref(request.attempt_id),
            "execution_spec_sha256": spec.digest,
            "execution_spec": spec.as_dict(),
            "bootstrap_sha256": request.bootstrap_sha256,
            "bootstrap_base64": base64.b64encode(request.bootstrap_bytes).decode("ascii"),
            "app_server_argv": self.build_argv(request),
            "thread_start_params": self._thread_start_params(spec, selected_roots),
            "polling_cadence_seconds": self.polling_cadence_seconds,
        }
        worker_request_sha256 = sha256_bytes(canonical_json_bytes(worker_request))
        worker_request_path = self._worker_request_path(request.attempt_id)
        write_json_once(worker_request_path, worker_request)
        launch = {
            "schema": "wc.codex_attempt_launch.v3",
            "attempt_id": request.attempt_id,
            "session_id": request.session_id,
            "provider_ref": self._provider_ref(request.attempt_id),
            "execution_spec_sha256": spec.digest,
            "bootstrap_sha256": request.bootstrap_sha256,
            "bootstrap_manifest_sha256": spec.bootstrap_manifest_sha256,
            "staged_inventory_sha256": staged_inventory_digest(spec.staged_attachments),
            "worker_request_sha256": worker_request_sha256,
        }
        write_json_once(launch_path, launch)
        launch_sha256 = sha256_bytes(canonical_json_bytes(launch))
        module = "research_attempt_adapter.codex_attempt_worker"
        argv = [
            sys.executable,
            "-m",
            module,
            "--state-directory",
            str(state_directory),
        ]
        kwargs: dict[str, object] = {
            "args": argv,
            # The package is installed into this interpreter's environment. A
            # worker must not depend on the caller's checkout layout or cwd.
            "cwd": str(state_directory),
            "env": self._environment,
            "stdin": subprocess.DEVNULL,
            "stdout": subprocess.DEVNULL,
            "stderr": subprocess.DEVNULL,
            "shell": False,
        }
        if os.name == "nt":
            kwargs["creationflags"] = (
                getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0x200)
                | getattr(subprocess, "DETACHED_PROCESS", 0x8)
            )
        else:
            kwargs["start_new_session"] = True
        try:
            worker = self._worker_launcher(**kwargs)
        except OSError as exc:
            raise ProviderRejectedError(
                "codex_worker_start_rejected", "The Attempt worker could not be started."
            ) from exc
        identity = self._identity_reader(int(worker.pid))
        if identity is None:
            raise ProviderEffectUnknownError(
                "codex_worker_identity_unknown",
                "The worker started but its process-birth identity could not be established.",
            )
        owner = {
            "schema": "wc.codex_attempt_worker_owner.v1",
            "attempt_id": request.attempt_id,
            "launch_binding_sha256": launch_sha256,
            "process_identity": identity,
        }
        write_json_once(self._owner_path(request.attempt_id), owner)
        return ProviderObservation(
            provider_id=self.provider_id,
            provider_ref=self._provider_ref(request.attempt_id),
            state=ProviderState.RUNNING,
            detail_code="codex_worker_running",
            resource_facts={"worker_pid": int(worker.pid)},
        )

    def request_cancel(self, request: ProviderRequest) -> ProviderObservation:
        return self._request_control(request, "cancel")

    def force_stop(self, request: ProviderRequest) -> ProviderObservation:
        return self._request_control(request, "force_stop")

    def reconcile(self, request: ProviderRequest) -> ProviderObservation:
        completion_path = self._completion_path(request.attempt_id)
        if completion_path.exists():
            return self._read_completion(
                completion_path, request.attempt_id, request.execution_spec_sha256
            )
        launch = self._validated_provider_launch(request)
        return self._observe_recorded_launch(request.attempt_id, launch)

    def _request_control(
        self, request: ProviderRequest, action: str
    ) -> ProviderObservation:
        completion_path = self._completion_path(request.attempt_id)
        if completion_path.exists():
            return self._read_completion(
                completion_path, request.attempt_id, request.execution_spec_sha256
            )
        launch = self._validated_provider_launch(request)
        control = {
            "schema": "wc.codex_attempt_control.v1",
            "attempt_id": request.attempt_id,
            "provider_ref": str(launch["provider_ref"]),
            "launch_binding_sha256": sha256_bytes(canonical_json_bytes(launch)),
            "action": action,
        }
        write_json_once(self._control_path(request.attempt_id, action), control)
        observation = self._observe_recorded_launch(request.attempt_id, launch)
        if observation.state is ProviderState.RUNNING:
            return ProviderObservation(
                provider_id=self.provider_id,
                provider_ref=observation.provider_ref,
                state=ProviderState.RUNNING,
                detail_code=(
                    "codex_cancel_requested"
                    if action == "cancel"
                    else "codex_force_stop_requested"
                ),
                resource_facts=dict(observation.resource_facts),
            )
        return observation

    def _observe_recorded_launch(
        self, attempt_id: str, launch: Mapping[str, object]
    ) -> ProviderObservation:
        completion_path = self._completion_path(attempt_id)
        if completion_path.exists():
            return self._read_completion(
                completion_path, attempt_id, str(launch["execution_spec_sha256"])
            )
        owner_path = self._owner_path(attempt_id)
        if not owner_path.exists():
            return ProviderObservation(
                provider_id=self.provider_id,
                provider_ref=str(launch["provider_ref"]),
                state=ProviderState.UNKNOWN,
                detail_code="codex_worker_owner_unavailable",
            )
        owner = read_json_object(owner_path)
        expected_launch_sha256 = sha256_bytes(canonical_json_bytes(launch))
        if (
            owner.get("schema") != "wc.codex_attempt_worker_owner.v1"
            or owner.get("attempt_id") != attempt_id
            or owner.get("launch_binding_sha256") != expected_launch_sha256
        ):
            raise ProviderEffectUnknownError(
                "codex_worker_owner_invalid", "The worker ownership record is invalid."
            )
        identity = owner.get("process_identity")
        if self._identity_matcher(identity):
            worker_pid = (
                identity.get("pid") if isinstance(identity, dict) else None
            )
            return ProviderObservation(
                provider_id=self.provider_id,
                provider_ref=str(launch["provider_ref"]),
                state=ProviderState.RUNNING,
                detail_code="codex_worker_running",
                resource_facts={"worker_pid": worker_pid},
            )
        if completion_path.exists():
            return self._read_completion(
                completion_path, attempt_id, str(launch["execution_spec_sha256"])
            )
        return ProviderObservation(
            provider_id=self.provider_id,
            provider_ref=str(launch["provider_ref"]),
            state=ProviderState.UNKNOWN,
            detail_code="codex_worker_exited_without_terminal_record",
        )

    def _thread_start_params(
        self, spec: ProviderExecutionSpec, resolved_local_roots: tuple[str, ...]
    ) -> dict[str, object]:
        selected = spec.selected_capabilities
        has_browser = selected.browser is not None
        has_plugins = any(
            root.kind is LocalCapabilityKind.PLUGIN for root in selected.local_roots
        )
        config: dict[str, object] = {
            "features.apps": bool(selected.apps),
            "features.browser_use": has_browser,
            "features.browser_use_external": has_browser,
            "features.computer_use": has_browser,
            "features.enable_mcp_apps": bool(selected.apps),
            "features.plugins": has_plugins,
            "features.browser_use_full_cdp_access": False,
            "features.goals": False,
            "features.in_app_browser": False,
            "features.multi_agent": "native_delegation" in spec.baseline_capabilities,
            "features.multi_agent_v2.enabled": False,
            "agents.default_subagent_model": spec.model_profile,
            "agents.default_subagent_reasoning_effort": spec.reasoning_effort,
            "agents.max_depth": _FORMAL_DELEGATION_MAX_DEPTH,
            "features.plugin_sharing": False,
            "features.remote_plugin": False,
            "features.remote_models": False,
            "features.rollout_budget": False,
            "features.shell_tool": "shell" in spec.baseline_capabilities,
            "features.skill_mcp_dependency_install": False,
            "features.token_budget": False,
            "orchestrator.skills.enabled": bool(selected.local_roots),
            "orchestrator.mcp.enabled": bool(selected.mcp_servers),
            "apps._default.enabled": False,
            "apps._default.destructive_enabled": False,
            "apps._default.open_world_enabled": False,
            "tools.experimental_request_user_input.enabled": False,
            "web_search": (
                "live"
                if "web_search" in spec.baseline_capabilities
                else "disabled"
            ),
        }
        selected_mcp = {item.server_id: item for item in selected.mcp_servers}
        for server_id in self._trusted_mcp_server_ids:
            key = f"mcp_servers.{json.dumps(server_id)}"
            selection = selected_mcp.get(server_id)
            config[f"{key}.enabled"] = selection is not None
            config[f"{key}.enabled_tools"] = (
                list(selection.enabled_tools) if selection is not None else []
            )
        selected_apps = {item.app_id: item for item in selected.apps}
        for app_id in self._trusted_app_ids:
            key = f"apps.{json.dumps(app_id)}"
            selection = selected_apps.get(app_id)
            config[f"{key}.enabled"] = selection is not None
            config[f"{key}.default_tools_enabled"] = False
            config[f"{key}.default_tools_approval_mode"] = "auto"
            config[f"{key}.destructive_enabled"] = False
            config[f"{key}.open_world_enabled"] = False
            if selection is not None:
                for tool in selection.enabled_tools:
                    tool_key = f"{key}.tools.{json.dumps(tool)}"
                    config[f"{tool_key}.enabled"] = True
                    config[f"{tool_key}.approval_mode"] = "auto"
        selected_capability_roots = [
            {
                "id": selection.id,
                "location": {
                    "type": "environment",
                    "environmentId": _LOCAL_ENVIRONMENT_ID,
                    "path": resolved_path,
                },
            }
            for selection, resolved_path in zip(
                selected.local_roots, resolved_local_roots, strict=True
            )
        ]
        baseline_labels = [
            label
            for capability, label in (
                ("shell", "secretless shell/Python"),
                ("web_search", "provider-hosted live web search"),
                ("native_delegation", "native delegation"),
            )
            if capability in spec.baseline_capabilities
        ]
        capability_guidance = (
            "Provider-native capabilities enabled for this Attempt: "
            + (", ".join(baseline_labels) if baseline_labels else "none")
            + ". Use their provider-supplied schemas as the usage authority; do not "
            "invent wrappers or guess unsupported calls."
        )
        delegation_guidance = (
            " Native delegation uses one clean hierarchy: this formal root may create "
            "branch researchers; a branch researcher may create leaf helpers; leaves "
            "cannot create descendants. Width remains adaptive rather than fixed, and "
            "each researcher synthesizes its leaf results before returning. Descendant "
            "final plaintext and direct parentage are retained automatically through "
            "the existing sealed Attempt output path."
            if "native_delegation" in spec.baseline_capabilities
            else ""
        )
        return {
            "cwd": spec.scratch_directory,
            "approvalPolicy": "never",
            "approvalsReviewer": "user",
            "developerInstructions": (
                "Execute only this Workstation-owned formal Attempt. Read the immutable "
                "bootstrap manifest and retain all factual work under the supplied output "
                "or scratch root. Do not treat operational execution as mathematical acceptance. "
                + capability_guidance
                + delegation_guidance
            ),
            "model": spec.model_profile,
            "modelProvider": _CODEX_MODEL_PROVIDER,
            "allowProviderModelFallback": False,
            "config": config,
            "dynamicTools": None,
            "environments": [
                {
                    "environmentId": _LOCAL_ENVIRONMENT_ID,
                    "cwd": spec.scratch_directory,
                }
            ],
            "selectedCapabilityRoots": selected_capability_roots,
            "runtimeWorkspaceRoots": [
                spec.scratch_directory,
                spec.output_directory,
                spec.source_working_directory,
                spec.input_staging_root,
                *resolved_local_roots,
            ],
            "permissions": _PERMISSION_ID,
            "experimentalRawEvents": False,
            "ephemeral": True,
        }

    def _require_execution_spec_policy(
        self, spec: ProviderExecutionSpec
    ) -> tuple[str, ...]:
        _require_execution_spec_cognitive_policy(spec)
        if not set(spec.baseline_capabilities).issubset(_BASELINE_CAPABILITIES):
            raise ProviderRejectedError(
                "codex_baseline_capability_unsupported",
                "baseline_capabilities names only shell, web search, or native delegation; selected capabilities carry every additional exact tool.",
            )
        selected = spec.selected_capabilities
        selected_mcp = {item.server_id for item in selected.mcp_servers}
        selected_apps = {item.app_id for item in selected.apps}
        if not selected_mcp.issubset(self._trusted_mcp_server_ids):
            raise ProviderRejectedError(
                "codex_untrusted_mcp_server",
                "Selected MCP server is not one server-side trusted preconfiguration.",
            )
        if not selected_apps.issubset(self._trusted_app_ids):
            raise ProviderRejectedError(
                "codex_untrusted_app",
                "Selected app is not one server-side trusted connector.",
            )
        network_capabilities_selected = bool(
            selected.mcp_servers or selected.apps or selected.browser is not None
        )
        if (
            (
                "web_search" in spec.baseline_capabilities
                or network_capabilities_selected
            )
            and spec.network_policy is not NetworkPolicy.PUBLIC
        ):
            raise ProviderRejectedError(
                "codex_capability_network_mismatch",
                "Live network capabilities require the verified PUBLIC network binding.",
            )
        if selected.browser is not None and (
            selected.browser
            is not BrowserCapabilityMode.ISOLATED_EPHEMERAL_UNAUTHENTICATED
        ):
            raise ProviderRejectedError(
                "codex_browser_policy_unsupported", "Browser selection is not isolated."
            )
        if spec.network_policy is NetworkPolicy.PUBLIC:
            if (
                self._outer_containment is None
                or spec.outer_containment != self._outer_containment
            ):
                raise ProviderRejectedError(
                    "codex_outer_containment_mismatch",
                    "PUBLIC execution does not match the provider's verified outer boundary.",
                )
        elif spec.outer_containment is not None:
            raise ProviderRejectedError(
                "codex_outer_containment_mismatch",
                "Denied execution cannot claim a public outer boundary.",
            )
        if spec.resource_request.provider_parameters:
            raise ProviderRejectedError(
                "codex_provider_parameters_unsupported",
                "Codex Attempt intent cannot carry commands, URLs, credentials, or opaque provider parameters.",
            )
        codex_home = self._environment.get("CODEX_HOME")
        if codex_home is not None and (
            spec.protected_root is None
            or Path(spec.protected_root).absolute() != Path(codex_home).absolute()
        ):
            raise ProviderRejectedError(
                "codex_protected_root_mismatch",
                "The outer CODEX_HOME must be the exact model-denied protected root.",
            )
        execution_roots = [
            self.state_root,
            Path(spec.source_working_directory),
            Path(spec.input_staging_root),
            Path(spec.scratch_directory),
            Path(spec.output_directory),
        ]
        if spec.protected_root is not None:
            execution_roots.append(Path(spec.protected_root))
        for index, first in enumerate(execution_roots):
            for second in execution_roots[index + 1 :]:
                if self._paths_overlap(first, second):
                    raise ProviderRejectedError(
                        "codex_execution_roots_overlap",
                        "Provider state, protected, readable, and writable roots must be disjoint.",
                    )
        source_root = Path(spec.source_working_directory).resolve(strict=True)
        try:
            model_catalog_path = self._require_model_catalog_path(
                self.model_catalog_path
            )
        except ContractError as exc:
            raise ProviderRejectedError(
                "codex_model_catalog_invalid",
                "The pinned Codex model catalog is no longer one safe release file.",
            ) from exc
        if source_root not in model_catalog_path.parents:
            raise ProviderRejectedError(
                "codex_model_catalog_outside_source",
                "The pinned Codex model catalog must be inside the immutable release.",
            )
        resolved_local_roots: list[str] = []
        seen_paths: set[str] = set()
        for selection in selected.local_roots:
            candidate = source_root.joinpath(*selection.release_relative_path.split("/"))
            try:
                candidate = candidate.resolve(strict=True)
            except OSError as exc:
                raise ProviderRejectedError(
                    "codex_capability_root_unavailable",
                    "Selected local capability root is unavailable.",
                ) from exc
            if candidate == source_root or source_root not in candidate.parents:
                raise ProviderRejectedError(
                    "codex_capability_root_escape",
                    "Selected local capability root escapes the immutable release.",
                )
            self._require_directory(candidate, "selected_capability_root")
            self._reject_links_below(candidate, "selected_capability_root")
            key = os.path.normcase(str(candidate))
            if key in seen_paths:
                raise ProviderRejectedError(
                    "codex_capability_root_duplicate",
                    "Selected local capability roots contain a duplicate path.",
                )
            seen_paths.add(key)
            resolved_local_roots.append(str(candidate))
        return tuple(resolved_local_roots)

    @classmethod
    def _verify_staged_inputs(cls, request: LaunchRequest) -> None:
        spec = request.execution_spec
        root = Path(spec.input_staging_root)
        cls._require_directory(root, "input_staging_root")
        manifest = Path(spec.bootstrap_manifest_path)
        cls._verify_file(
            manifest, spec.bootstrap_manifest_sha256, None, "bootstrap_manifest"
        )
        expected = {str(manifest.absolute()).casefold()}
        for item in spec.staged_attachments:
            path = Path(staged_attachment_path(str(root), item.logical_name))
            if Path(item.staged_path) != path:
                raise ProviderRejectedError(
                    "codex_staged_layout_mismatch", "Staged input path is inconsistent."
                )
            cls._verify_file(path, item.sha256, item.byte_length, "staged_input")
            expected.add(str(path.absolute()).casefold())
        actual = {
            str((Path(directory) / name).absolute()).casefold()
            for directory, _directories, files in os.walk(root, followlinks=False)
            for name in files
        }
        for directory, directories, _files in os.walk(root, followlinks=False):
            for name in directories:
                cls._require_directory(Path(directory) / name, "staged_directory")
        if actual != expected:
            raise ProviderRejectedError(
                "codex_staged_inventory_mismatch",
                "Staged input contains missing or extra files.",
            )

    @staticmethod
    def _validate_bootstrap(request: LaunchRequest) -> None:
        try:
            payload = json.loads(request.bootstrap_bytes.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ProviderRejectedError(
                "codex_bootstrap_invalid", "Attempt bootstrap is not canonical JSON."
            ) from exc
        spec = request.execution_spec
        if (
            not isinstance(payload, dict)
            or canonical_json_bytes(payload) != request.bootstrap_bytes
            or set(payload)
            != {
                "schema",
                "attempt_id",
                "session_id",
                "manifest_path",
                "output_root",
                "scratch_root",
                "instruction",
            }
            or payload.get("schema") != "wc.formal_attempt_bootstrap.v2"
            or payload.get("attempt_id") != request.attempt_id
            or payload.get("session_id") != request.session_id
            or payload.get("manifest_path") != spec.bootstrap_manifest_path
            or payload.get("output_root") != spec.output_directory
            or payload.get("scratch_root") != spec.scratch_directory
            or not isinstance(payload.get("instruction"), str)
            or not payload["instruction"]
        ):
            raise ProviderRejectedError(
                "codex_bootstrap_binding_mismatch",
                "Attempt bootstrap does not bind the exact manifest and custody roots.",
            )

    @classmethod
    def _verify_file(
        cls, path: Path, digest: str, size: int | None, role: str
    ) -> None:
        require_no_link_components(path, role)
        try:
            details = path.lstat()
        except OSError as exc:
            raise ProviderRejectedError(
                "codex_input_unavailable", f"{role} unavailable."
            ) from exc
        if (
            cls._is_link_or_reparse(details)
            or not stat.S_ISREG(details.st_mode)
            or details.st_nlink != 1
            or (size is not None and details.st_size != size)
            or sha256_file(path) != digest
        ):
            raise ProviderRejectedError(
                "codex_input_custody_mismatch",
                f"{role} failed exact custody verification.",
            )

    @classmethod
    def _require_directory(cls, path: Path, role: str) -> None:
        require_no_link_components(path, role)
        try:
            details = path.lstat()
        except OSError as exc:
            raise ContractError("unsafe_directory", f"{role} is unavailable.") from exc
        if cls._is_link_or_reparse(details) or not stat.S_ISDIR(details.st_mode):
            raise ContractError("unsafe_directory", f"{role} must be a non-link directory.")

    @classmethod
    def _reject_links_below(cls, root: Path, role: str) -> None:
        for directory, directories, files in os.walk(root, followlinks=False):
            for name in [*directories, *files]:
                try:
                    details = (Path(directory) / name).lstat()
                except OSError as exc:
                    raise ProviderRejectedError(
                        "codex_capability_root_unobservable",
                        "Selected local capability root changed during validation.",
                    ) from exc
                if cls._is_link_or_reparse(details):
                    raise ProviderRejectedError(
                        "codex_capability_root_link",
                        f"{role} contains a link or reparse point.",
                    )

    def _validated_provider_launch(
        self, request: ProviderRequest
    ) -> dict[str, object]:
        launch_path = self._launch_path(request.attempt_id)
        if not launch_path.exists():
            raise ProviderEffectUnknownError(
                "codex_launch_state_missing", "The durable Codex launch is unavailable."
            )
        launch = read_json_object(launch_path)
        expected = {
            "schema": "wc.codex_attempt_launch.v3",
            "attempt_id": request.attempt_id,
            "session_id": request.session_id,
            "provider_ref": request.provider_ref or self._provider_ref(request.attempt_id),
            "execution_spec_sha256": request.execution_spec_sha256,
            "bootstrap_manifest_sha256": request.bootstrap_manifest_sha256,
            "staged_inventory_sha256": staged_inventory_digest(request.staged_attachments),
        }
        if any(launch.get(key) != value for key, value in expected.items()):
            raise ProviderRejectedError(
                "codex_launch_binding_mismatch",
                "Provider request does not bind the durable Attempt worker.",
            )
        return launch

    def _validate_launch_binding(
        self, launch: Mapping[str, object], request: LaunchRequest
    ) -> None:
        expected = {
            "schema": "wc.codex_attempt_launch.v3",
            "attempt_id": request.attempt_id,
            "session_id": request.session_id,
            "provider_ref": self._provider_ref(request.attempt_id),
            "execution_spec_sha256": request.execution_spec.digest,
            "bootstrap_sha256": request.bootstrap_sha256,
            "bootstrap_manifest_sha256": request.execution_spec.bootstrap_manifest_sha256,
            "staged_inventory_sha256": staged_inventory_digest(
                request.execution_spec.staged_attachments
            ),
        }
        if any(launch.get(key) != value for key, value in expected.items()):
            raise ProviderRejectedError(
                "codex_launch_binding_mismatch",
                "Persisted Codex launch binding drifted.",
            )
        worker_request_path = self._worker_request_path(request.attempt_id)
        if (
            not worker_request_path.is_file()
            or launch.get("worker_request_sha256") != sha256_file(worker_request_path)
        ):
            raise ProviderRejectedError(
                "codex_worker_request_binding_mismatch",
                "Persisted worker request drifted.",
            )

    def _read_completion(
        self, path: Path, attempt_id: str, execution_spec_sha256: str
    ) -> ProviderObservation:
        payload = read_json_object(path)
        launch = read_json_object(self._launch_path(attempt_id))
        if (
            payload.get("schema") != "wc.codex_attempt_completion.v3"
            or payload.get("attempt_id") != attempt_id
            or payload.get("execution_spec_sha256") != execution_spec_sha256
            or payload.get("launch_binding_sha256")
            != sha256_bytes(canonical_json_bytes(launch))
            or not isinstance(payload.get("observation"), dict)
        ):
            raise ProviderEffectUnknownError(
                "codex_completion_invalid", "Codex completion facts are invalid."
            )
        try:
            observation = ProviderObservation.from_dict(payload["observation"])
        except (ContractError, KeyError, TypeError, ValueError) as exc:
            raise ProviderEffectUnknownError(
                "codex_completion_invalid", "Codex terminal observation is invalid."
            ) from exc
        if (
            observation.provider_id != self.provider_id
            or observation.provider_ref != self._provider_ref(attempt_id)
            or observation.state
            not in {
                ProviderState.SUCCEEDED,
                ProviderState.FAILED,
                ProviderState.CANCELLED,
                ProviderState.FORCE_STOPPED,
            }
        ):
            raise ProviderEffectUnknownError(
                "codex_completion_invalid", "Codex terminal observation is invalid."
            )
        return observation

    @staticmethod
    def _trusted_ids(values: Sequence[str], field_name: str) -> frozenset[str]:
        if isinstance(values, (str, bytes)):
            raise ContractError("invalid_trusted_capabilities", f"{field_name} is invalid.")
        normalized = tuple(
            require_capability_identifier(item, f"{field_name}[{index}]")
            for index, item in enumerate(values)
        )
        if len(normalized) != len(set(normalized)):
            raise ContractError(
                "invalid_trusted_capabilities", f"{field_name} contains duplicates."
            )
        return frozenset(normalized)

    def _state_directory(self, attempt_id: str) -> Path:
        return self.state_root / attempt_id

    def _launch_path(self, attempt_id: str) -> Path:
        return self._state_directory(attempt_id) / "launch.json"

    def _worker_request_path(self, attempt_id: str) -> Path:
        return self._state_directory(attempt_id) / "worker-request.json"

    def _owner_path(self, attempt_id: str) -> Path:
        return self._state_directory(attempt_id) / "worker-owner.json"

    def _completion_path(self, attempt_id: str) -> Path:
        return self._state_directory(attempt_id) / "completion.json"

    def _control_path(self, attempt_id: str, action: str) -> Path:
        return self._state_directory(attempt_id) / f"{action}.request.json"

    @staticmethod
    def _provider_ref(attempt_id: str) -> str:
        return f"codex:{attempt_id}"

    @staticmethod
    def _forward(path: str | Path) -> str:
        return str(Path(path).absolute()).replace("\\", "/")

    @staticmethod
    def _paths_overlap(first: Path, second: Path) -> bool:
        first_abs = Path(os.path.normcase(os.path.abspath(first)))
        second_abs = Path(os.path.normcase(os.path.abspath(second)))
        return (
            first_abs == second_abs
            or first_abs in second_abs.parents
            or second_abs in first_abs.parents
        )

    @classmethod
    def _require_model_catalog_path(cls, value: str | Path) -> Path:
        candidate = Path(value)
        if not candidate.is_absolute():
            raise ContractError(
                "invalid_model_catalog",
                "model_catalog_path must be one absolute pinned release file.",
            )
        normalized = Path(os.path.abspath(candidate))
        try:
            require_no_link_components(normalized, "model_catalog")
            normalized = normalized.resolve(strict=True)
            require_no_link_components(normalized, "model_catalog")
            details = normalized.lstat()
        except (OSError, ProviderEffectUnknownError) as exc:
            raise ContractError(
                "invalid_model_catalog",
                "model_catalog_path must be one available non-link release file.",
            ) from exc
        if (
            cls._is_link_or_reparse(details)
            or not stat.S_ISREG(details.st_mode)
            or details.st_nlink != 1
        ):
            raise ContractError(
                "invalid_model_catalog",
                "model_catalog_path must be one single-link regular release file.",
            )
        return normalized

    @staticmethod
    def _resolve_executable(executable: str) -> str:
        resolved = shutil.which(executable)
        if resolved is None:
            candidate = Path(executable)
            if candidate.is_absolute() and candidate.exists():
                resolved = str(candidate)
            else:
                raise ContractError(
                    "codex_executable_unavailable", "Codex executable was not found."
                )
        path = Path(resolved)
        if os.name == "nt" and path.suffix.casefold() == ".cmd":
            native = (
                path.parent
                / "node_modules"
                / "@openai"
                / "codex"
                / "node_modules"
                / "@openai"
                / "codex-win32-x64"
                / "vendor"
                / "x86_64-pc-windows-msvc"
                / "bin"
                / "codex.exe"
            )
            if path.name.casefold() == "codex.cmd" and native.is_file():
                return str(native)
        return str(path)

    @staticmethod
    def _is_link_or_reparse(details: os.stat_result) -> bool:
        reparse = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
        return stat.S_ISLNK(details.st_mode) or bool(
            getattr(details, "st_file_attributes", 0) & reparse
        )
