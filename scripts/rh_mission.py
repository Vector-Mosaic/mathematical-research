#!/usr/bin/env python3
"""CLI for the noncanonical RH Mission application facade."""

from __future__ import annotations

import argparse
import json
import math
import os
import re
import signal
import sqlite3
import sys
from pathlib import Path
from typing import Any, Mapping, Sequence


REPO_ROOT = Path(__file__).resolve().parents[1]
PACKAGE_ROOT = (
    REPO_ROOT / "packages" / "research-core"
)
ADAPTER_PACKAGE_ROOT = REPO_ROOT / "packages" / "research-attempt-adapter"
for package_root in (REPO_ROOT, PACKAGE_ROOT, ADAPTER_PACKAGE_ROOT):
    if str(package_root) not in sys.path:
        sys.path.insert(0, str(package_root))

from research_core.canonical_snapshot import load_canonical_snapshot  # noqa: E402
from research_core.json_support import (
    loads_strict_json_object,
)
from research_core.evidence_store import (
    IntegrityFreeze,
)
from research_core.mission_operation_contract import (  # noqa: E402
    MISSION_SEMANTIC_REQUEST_SCHEMA_VERSION,
    MISSION_SEMANTIC_RESULT_SCHEMA_VERSION,
    MissionOperationContractError,
    operation_from_cli_verb,
    project_cli_operations,
    project_model_operations,
    project_operation_capabilities,
    validate_admission_case_request,
    validate_admission_decision_grant_request,
    validate_admission_decision_request,
    validate_admission_review_grant_request,
    validate_admission_review_request,
    validate_candidate_a1_review_grant_request,
    validate_candidate_a1_review_request,
    validate_historical_read_grant_request,
    validate_historical_read_request,
    validate_research_read_grant_request,
    validate_research_read_request,
    validate_research_read_result,
    validate_semantic_request,
    validate_semantic_result,
)
from research_core.mission_interface import (  # noqa: E402
    CheckpointCommittedSourceHandoffError,
    MissionInterface,
    MissionInterfaceError,
)
from research_core.mission_observation import validate_observation_request  # noqa: E402
from research_core.observability import (
    CaptureWorkspaceObserver,
    EventKind,
    ObservationEnvironment,
)
from research_core.mission_attempt_runtime import (  # noqa: E402
    FormalAttemptBridgeError,
    FormalAttemptControl,
    FormalAttemptRuntimeFacts,
    build_formal_attempt_adapter,
    execute_formal_attempt_operation,
)
from research_core.research_model import deep_thaw  # noqa: E402
from research_core.validator import _schema_violations  # noqa: E402
from research_core.workspace_schema import (
    IdentityKind,
    TypedWorkspaceId,
)
from research_core.workspace_store import (  # noqa: E402
    CheckpointSourceError,
    CheckpointSourceReacquisitionError,
    CommandConflictError,
    MISSION_FENCE_DIAGNOSTIC_STAGES,
    MissionFenceStaleCommandError,
    StaleCommandError,
    StaleWriterError,
    WorkspaceIntegrityError,  # noqa: F401 - exported through this loaded tool module in tests
    WorkspaceStore,
    WorkspaceStoreError,
    mission_fence_diagnostic_stage,
)
from research_core.mission_owner import MissionOwnerError  # noqa: E402
from research_attempt_adapter import ResearchAttemptAdapterError  # noqa: E402


TOOL_ID = "tool.mathematical_research.rh_mission"
ENTRYPOINT = "python scripts/rh_mission.py"
MISSION_HOST_BRIDGE_REQUEST_SCHEMA_VERSION = (
    "mathematical_research.mission_host_bridge_request.v1"
)
MISSION_OWNER_GENESIS_REQUEST_SCHEMA_VERSION = (
    "mathematical_research.mission_owner_genesis_request.v1"
)
MISSION_RELEASE_RECORD_SCHEMA_VERSION = "wc.rh_mission_host_release.v1"
MISSION_RELEASE_RECORD_RELATIVE_PATH = Path(".mathematical-research-release.json")
MISSION_RELEASE_RECORD_PATH = REPO_ROOT / MISSION_RELEASE_RECORD_RELATIVE_PATH
MISSION_SEED_RELATIVE_PATH = Path(
    "contracts/rh_autonomous_mission_seed.v1.json"
)
_FULL_GIT_COMMIT = re.compile(r"^[0-9a-f]{40}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_CHECKPOINT_SOURCE_WARNING_CODES = frozenset(
    {
        "checkpoint_source_capture_failed_writer_reacquired",
        "checkpoint_source_operation_teardown_failed_writer_revalidated",
        "checkpoint_source_snapshot_skipped_writer_preserved",
    }
)
_CHECKPOINT_SOURCE_PRESERVED_WRITER_REASONS = frozenset(
    {
        "checkpoint_source_busy",
        "checkpoint_source_measurement_unavailable",
        "checkpoint_source_writer_release_failed",
    }
)
_CHECKPOINT_SOURCE_TEARDOWN_REASONS = frozenset(
    {
        "checkpoint_source_published",
        "checkpoint_source_capture_failed",
        *_CHECKPOINT_SOURCE_PRESERVED_WRITER_REASONS,
    }
)

_CLI_PROJECTION = deep_thaw(project_cli_operations())
_CLI_ITEMS_BY_VERB = {
    item["verb"]: item for item in _CLI_PROJECTION["verbs"]
}
_MUTATING_COMMANDS = frozenset(
    item["verb"] for item in _CLI_PROJECTION["verbs"] if not item["read_only"]
)

_HOST_BRIDGE_ACTIONS: Mapping[str, Mapping[str, Any]] = {
    "authorize_executive_epoch": {
        "owner_method": "authorize_executive_epoch_from_owner",
        "payload_keys": {"expected_cut"},
    },
    "validate_suspended_epoch_cut": {
        "owner_method": "validate_suspended_epoch_cut_from_owner",
        "payload_keys": {
            "expected_cut",
            "executiveEpochId",
            "rootThreadId",
            "workspaceRoot",
        },
    },
    "bind_executive_epoch": {
        "owner_method": "bind_executive_epoch_from_owner",
        "payload_keys": {
            "executiveEpochId",
            "rootThreadId",
            "workspaceRoot",
        },
    },
    "execute_semantic_operation": {
        "owner_method": "execute_semantic_operation",
        "payload_keys": {"request", "binding"},
        "optional_payload_keys": {"root_query_context"},
        "binding_keys": {
            "rootThreadId",
            "executiveEpochId",
        },
    },
    "issue_research_read_grant": {
        "owner_method": "issue_research_read_grant",
        "payload_keys": {"request", "binding"},
        "binding_keys": {"rootThreadId", "executiveEpochId", "childThreadId", "parentThreadId", "depth"},
    },
    "execute_research_read": {
        "owner_method": "execute_research_read",
        "payload_keys": {"request", "grant", "binding", "research_query_context"},
        "binding_keys": {"rootThreadId", "executiveEpochId", "callerThreadId", "parentThreadId", "depth", "turnId", "grantId", "assignmentId"},
    },
    "issue_historical_read_grant": {
        "owner_method": "issue_historical_read_grant",
        "payload_keys": {"request", "binding"},
        "binding_keys": {
            "rootThreadId",
            "executiveEpochId",
            "childThreadId",
            "parentThreadId",
            "depth",
        },
    },
    "execute_delegated_read": {
        "owner_method": "execute_delegated_read",
        "payload_keys": {"request", "grant", "binding"},
        "binding_keys": {
            "rootThreadId",
            "executiveEpochId",
            "callerThreadId",
            "parentThreadId",
            "depth",
            "turnId",
            "grantId",
        },
    },
    "issue_candidate_a1_review_grant": {
        "owner_method": "issue_candidate_a1_review_grant",
        "payload_keys": {"request", "binding"},
        "binding_keys": {
            "rootThreadId",
            "executiveEpochId",
            "childThreadId",
            "parentThreadId",
            "depth",
        },
    },
    "execute_candidate_a1_review": {
        "owner_method": "execute_candidate_a1_review",
        "payload_keys": {"request", "grant", "binding"},
        "binding_keys": {
            "rootThreadId",
            "executiveEpochId",
            "callerThreadId",
            "parentThreadId",
            "depth",
            "turnId",
        },
    },
    "open_complete_claim_admission_case": {
        "owner_method": "open_complete_claim_admission_case",
        "payload_keys": {"request", "binding"},
        "binding_keys": {"rootThreadId", "executiveEpochId"},
    },
    "issue_admission_review_grant": {
        "owner_method": "issue_admission_review_grant",
        "payload_keys": {"request", "binding"},
        "binding_keys": {
            "rootThreadId",
            "executiveEpochId",
            "childThreadId",
            "parentThreadId",
            "depth",
        },
    },
    "issue_admission_decision_grant": {
        "owner_method": "issue_admission_decision_grant",
        "payload_keys": {"request", "binding"},
        "binding_keys": {
            "rootThreadId",
            "executiveEpochId",
            "childThreadId",
            "parentThreadId",
            "depth",
        },
    },
    "execute_admission_review": {
        "owner_method": "execute_admission_review",
        "payload_keys": {"request", "grant", "binding"},
        "binding_keys": {
            "rootThreadId",
            "executiveEpochId",
            "callerThreadId",
            "parentThreadId",
            "depth",
            "turnId",
        },
    },
    "execute_admission_decision": {
        "owner_method": "execute_admission_decision",
        "payload_keys": {"request", "grant", "binding"},
        "binding_keys": {
            "rootThreadId",
            "executiveEpochId",
            "callerThreadId",
            "parentThreadId",
            "depth",
            "turnId",
        },
    },
    "execute_formal_attempt": {
        "owner_method": "formal_attempt_runtime",
        "payload_keys": {"request", "binding", "runtime"},
        "binding_keys": {
            "rootThreadId",
            "executiveEpochId",
        },
    },
    "reconstruct": {
        "owner_method": "reconstruct",
        "payload_keys": set(),
        "optional_payload_keys": {"selected_readback_handles"},
    },
    "host_snapshot": {
        "owner_method": "host_snapshot",
        "payload_keys": set(),
        "optional_payload_keys": {"canonical_source"},
    },
    "observe_mission": {
        "owner_method": "observe_mission",
        "payload_keys": {"request", "cursor_mac_key"},
        "optional_payload_keys": {"canonical_source"},
    },
    "capture_native_material_observation": {
        "owner_method": "capture_native_material_observation",
        "payload_keys": {"observation", "binding"},
        "binding_keys": {
            "rootThreadId",
            "executiveEpochId",
        },
    },
    "fence_mission": {
        "owner_method": "fence_mission_from_owner",
        "payload_keys": {"context", "binding"},
        "binding_keys": {"executiveEpochId"},
    },
    "record_direct_failed_executive_epoch": {
        "owner_method": "record_direct_failed_executive_epoch_from_owner",
        "payload_keys": {"executiveEpochId", "reconciliation"},
    },
}

_HOST_BRIDGE_KEYS = {"schema_version", "action", "binding", "payload"}
_HOST_BRIDGE_OUTER_BINDING_KEYS = {
    "project_id",
    "mission_id",
}
_FORMAL_ATTEMPT_REQUEST_KEYS = {
    "selected_bet_sha256",
    "correction_basis",
}
_FORMAL_ATTEMPT_RUNTIME_KEYS = {
    "release_root",
    "release_commit",
    "state_root",
    "codex_executable",
    "codex_home",
    "outer_containment_id",
    "trusted_mcp_server_ids",
    "trusted_app_ids",
    "polling_cadence_seconds",
}
def _usage_contract() -> dict[str, Any]:
    return {
        "schema_version": "tool_usage.v1",
        "tool_id": TOOL_ID,
        "status": "ok",
        "purpose": (
            "Exercise the compact semantic RH Mission console through the sole "
            "Mathematical Research application facade."
        ),
        "entrypoint": ENTRYPOINT,
        "stability": "experimental",
        "lifecycle": "noncanonical_runtime",
        "safe_first_calls": [
            {"label": "usage", "cmd": f"{ENTRYPOINT} usage --format json"},
            {
                "label": "capabilities",
                "cmd": f"{ENTRYPOINT} capabilities --format json",
            },
        ],
        "verbs": [
            {"name": "usage", "side_effects": "none"},
            {"name": "capabilities", "side_effects": "none"},
            {
                "name": "owner-genesis",
                "side_effects": (
                    "create one absent noncanonical Mission workspace from one "
                    "exact owner request"
                ),
            },
            {
                "name": "owner-migrate-model-policy",
                "side_effects": (
                    "append the exact inactive Mission Sol/ultra to Astra/ultra "
                    "owner revision; no Goal, provider, or mathematical effect"
                ),
            },
            {
                "name": "owner-bind-scientific-context",
                "side_effects": (
                    "append only one inactive Mission scientific Context binding "
                    "from exact owner-selected preimages; no Context creation, "
                    "Strategy, canonical, Goal, or provider effect; no dry-run"
                ),
            },
            {
                "name": "owner-revise-scientific-context",
                "side_effects": "append only an exact patch to the already-bound scientific Context under stopped owner authority; no Mission binding, Strategy, canonical, Epoch, provider or service effect",
            },
            {
                "name": "owner-reauthorize-mission",
                "side_effects": (
                    "append one explicit new Mission grant after its exact fatal "
                    "fence and failed terminal; no Goal, provider, or mathematical effect"
                ),
            },
            {
                "name": "host-bridge",
                "side_effects": (
                    "Mission Host actions through the current Mission owner only"
                ),
            },
            *[
                {
                    "name": item["verb"],
                    "side_effects": (
                        "none; direct execution is blocked and only --dry-run is available"
                        if not item["read_only"]
                        else "none"
                    ),
                }
                for item in _CLI_PROJECTION["verbs"]
            ],
        ],
        "selectors": [
            {
                "name": "--workspace-root",
                "required": "all domain commands",
                "summary": (
                    "Exact existing Mission root; owner-genesis instead requires "
                    "the selected root to be absent."
                ),
            },
            {
                "name": "--project-id",
                "required": "all domain commands",
                "summary": "Expected immutable project identity.",
            },
            {
                "name": "--mission-id",
                "required": "all domain commands",
                "summary": "Expected immutable Mission identity.",
            },
            {
                "name": "--request",
                "required": "all canonical semantic, Host bridge, and owner operations",
                "summary": (
                    "Strict JSON file using the selected semantic, trusted Host "
                    "bridge, or private owner schema."
                ),
            },
            {
                "name": "--response",
                "required": "host-bridge",
                "summary": "Private response file adjacent to the Host request file.",
            },
            {
                "name": "--dry-run",
                "required": False,
                "summary": "Validate without issuing reusable authority or mutating state.",
            },
        ],
        "output_formats": ["text", "json"],
        "side_effects": {
            "writes_repo": False,
            "writes_paths": [
                "caller-selected owner-authorized Mission workspace only",
            ],
            "network_access": "none",
            "canonical_effect": "none",
            "public_effect": "none",
            "provider_effect": "none",
            "supports_dry_run": True,
        },
        "restrictions": {
            "real_mission_activation": True,
            "real_mission_genesis_route": "owner-genesis",
            "disposable_initialization": False,
            "caller_supplied_actor_lease_seal_or_cas_path": False,
            "caller_supplied_identity_state_clock_hash_or_custody_fact": False,
            "strategy_ranking_or_prompt_generation": False,
            "codex_process_control": False,
            "host_bridge_is_model_capability": False,
            "host_bridge_provider_or_runtime_control": False,
            "direct_semantic_mutation": False,
            "mcp_interface": False,
            "interface_readiness_claim": False,
        },
        "examples": [
            {
                "label": "Orient to the Mission panorama",
                "cmd": (
                    f"{ENTRYPOINT} orient --workspace-root C:\\path\\to\\disposable "
                    "--project-id project.rh --mission-id mission.1 "
                    "--request C:\\path\\orient.json --format json"
                ),
            },
            {
                "label": "Preview one Strategy judgment",
                "cmd": (
                    f"{ENTRYPOINT} record-strategy --workspace-root C:\\path\\to\\disposable "
                    "--project-id project.rh --mission-id mission.1 "
                    "--request C:\\path\\strategy.json --dry-run --format json"
                ),
            },
        ],
        "errors": [
            {
                "code": "invalid_invocation",
                "meaning": "Required selectors or strict input are missing or malformed.",
            },
            {
                "code": "workspace_error",
                "meaning": "The workspace identity, integrity, or current-state check failed.",
            },
            {
                "code": "mission_host_bridge_required",
                "meaning": (
                    "Actual semantic mutation requires the Mission Host bridge "
                    "and its current Goal binding."
                ),
            },
            {
                "code": "mission_cli_operation_mismatch",
                "meaning": "The semantic request operation differs from its CLI verb.",
            },
        ],
        "docs": [
            "docs/architecture.md",
            "packages/research-core/README.md",
        ],
        "semantic_request_schema_version": MISSION_SEMANTIC_REQUEST_SCHEMA_VERSION,
        "semantic_result_schema_version": MISSION_SEMANTIC_RESULT_SCHEMA_VERSION,
        "model_projection": deep_thaw(project_model_operations()),
        "cli_projection": deep_thaw(project_cli_operations()),
        "host_bridge": {
            "schema_version": MISSION_HOST_BRIDGE_REQUEST_SCHEMA_VERSION,
            "actions": {
                action: {"owner_method": item["owner_method"]}
                for action, item in _HOST_BRIDGE_ACTIONS.items()
            },
            "model_visible": False,
        },
    }


def _extract_global_options(
    argv: Sequence[str],
) -> tuple[list[str], dict[str, str | None]]:
    cleaned: list[str] = []
    options: dict[str, str | None] = {
        "workspace_root": None,
        "project_id": None,
        "mission_id": None,
        "format": "text",
    }
    option_map = {
        "--workspace-root": "workspace_root",
        "--project-id": "project_id",
        "--mission-id": "mission_id",
        "--format": "format",
    }
    index = 0
    while index < len(argv):
        item = argv[index]
        if item in option_map:
            if index + 1 >= len(argv):
                raise ValueError(f"{item} requires one value")
            options[option_map[item]] = argv[index + 1]
            index += 2
            continue
        if item == "--json":
            options["format"] = "json"
            index += 1
            continue
        cleaned.append(item)
        index += 1
    if options["format"] not in {"text", "json"}:
        raise ValueError("--format must be text or json")
    return cleaned, options


class _ArgumentParser(argparse.ArgumentParser):
    def error(self, message: str) -> None:
        raise ValueError(message)


def _parser() -> argparse.ArgumentParser:
    parser = _ArgumentParser(
        prog=ENTRYPOINT,
        description="Experimental noncanonical RH Mission facade.",
    )
    subparsers = parser.add_subparsers(
        dest="verb",
        required=True,
        parser_class=_ArgumentParser,
    )
    subparsers.add_parser("usage")
    subparsers.add_parser("capabilities")
    genesis = subparsers.add_parser("owner-genesis")
    genesis.add_argument("--request", required=True)
    migration = subparsers.add_parser("owner-migrate-model-policy")
    migration.add_argument("--request", required=True)
    scientific_binding = subparsers.add_parser("owner-bind-scientific-context")
    scientific_binding.add_argument("--request", required=True)
    scientific_revision = subparsers.add_parser("owner-revise-scientific-context")
    scientific_revision.add_argument("--request", required=True)
    reauthorization = subparsers.add_parser("owner-reauthorize-mission")
    reauthorization.add_argument("--request", required=True)
    bridge = subparsers.add_parser("host-bridge")
    bridge.add_argument("--request", required=True)
    bridge.add_argument("--response", required=True)
    for item in _CLI_PROJECTION["verbs"]:
        command = subparsers.add_parser(item["verb"])
        command.add_argument("--request", required=True)
        if item["supports_dry_run"]:
            command.add_argument("--dry-run", action="store_true")
    return parser


def _read_file(path_value: str, *, label: str) -> bytes:
    path = Path(path_value)
    if not path.is_absolute():
        raise ValueError(f"{label} must be an absolute path")
    if path.is_symlink():
        raise ValueError(f"{label} must be one existing non-symlink file")
    try:
        resolved = path.resolve(strict=True)
    except OSError as exc:
        raise ValueError(f"{label} cannot be resolved: {exc}") from exc
    if not resolved.is_file():
        raise ValueError(f"{label} must be one existing non-symlink file")
    return resolved.read_bytes()


def _read_request(path_value: str) -> Mapping[str, Any]:
    raw = _read_file(
        path_value,
        label="--request",
    )
    try:
        return loads_strict_json_object(raw)
    except (TypeError, ValueError, UnicodeError) as exc:
        raise ValueError(f"--request is not one strict UTF-8 JSON object: {exc}") from exc


def _result(
    *,
    status: str,
    verb: str,
    summary: str,
    data: Mapping[str, Any] | None = None,
    warnings: list[dict[str, Any]] | None = None,
    errors: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    return {
        "schema_version": "tool_result.v1",
        "tool_id": TOOL_ID,
        "status": status,
        "verb": verb,
        "summary": summary,
        "data": dict(data or {}),
        "warnings": warnings or [],
        "errors": errors or [],
    }


def _mission_fence_error_details(exc: Exception) -> list[dict[str, str]]:
    """Preserve fixed diagnostic facts in the existing error-code projection."""

    stage = getattr(exc, "_mission_fence_stage", None)
    if not isinstance(stage, str) or stage not in MISSION_FENCE_DIAGNOSTIC_STAGES:
        return []
    codes = [f"mission_fence_stage_{stage}"]
    for error_type, category in (
        (MissionOwnerError, "mission_owner"),
        (MissionInterfaceError, "mission_interface"),
        (StaleCommandError, "stale_command"),
        (CommandConflictError, "command_conflict"),
        (StaleWriterError, "stale_writer"),
        (WorkspaceIntegrityError, "workspace_integrity"),
        (WorkspaceStoreError, "workspace_store"),
        (TypeError, "type"),
        (ValueError, "value"),
        (OSError, "os"),
    ):
        if isinstance(exc, error_type):
            codes.append(f"mission_fence_error_{category}")
            break
    if isinstance(exc, MissionFenceStaleCommandError):
        if (
            isinstance(exc.diagnostic_code, str)
            and exc.diagnostic_code in MissionFenceStaleCommandError.CODES
        ):
            codes.append(exc.diagnostic_code)
    if isinstance(exc, MissionOwnerError) and isinstance(exc.code, str) and exc.code in {
        "mission_contract_invalid",
        "successor_mission_contract_invalid",
        "mission_lifecycle_invalid",
    }:
        codes.append(f"mission_fence_{exc.code}")
    return [
        {"code": code, "message": "Safe Mission fence failure diagnostic."}
        for code in codes
    ]


def _bridge_response_path(request_value: str, response_value: str) -> Path:
    request = Path(request_value)
    response = Path(response_value)
    if not response.is_absolute():
        raise ValueError("--response must be an absolute path")
    if response.exists() or response.is_symlink():
        raise ValueError("--response must be one absent non-symlink file")
    if response.parent.resolve(strict=True) != request.resolve(strict=True).parent:
        raise ValueError("--response must be adjacent to the private Host request file")
    return response


def _emit(
    payload: Mapping[str, Any],
    output_format: str,
    *,
    response_path: Path | None = None,
) -> None:
    if response_path is not None:
        if output_format != "json":
            raise ValueError("host-bridge response transport requires JSON format")
        descriptor = os.open(
            response_path,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL,
            0o600,
        )
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as stream:
            json.dump(payload, stream, indent=2, sort_keys=True, allow_nan=False)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        return
    if output_format == "json":
        print(json.dumps(payload, indent=2, sort_keys=True, allow_nan=False))
        return
    if payload.get("schema_version") == "tool_usage.v1":
        print(f"{TOOL_ID}: {payload['purpose']}")
        print("stability: experimental; lifecycle: noncanonical_runtime")
        print("verbs: " + ", ".join(item["name"] for item in payload["verbs"]))
        print(f"safe first call: {ENTRYPOINT} usage --format json")
        return
    print(f"{payload['status']}: {payload['summary']}")
    for key, value in payload.get("data", {}).items():
        print(f"{key}: {value}")
    for error in payload.get("errors", []):
        print(f"error[{error['code']}]: {error['message']}")


def _require_workspace_options(options: Mapping[str, str | None]) -> tuple[str, str, str]:
    values = (
        options["workspace_root"],
        options["project_id"],
        options["mission_id"],
    )
    if any(not isinstance(value, str) or not value.strip() for value in values):
        raise ValueError(
            "domain commands require --workspace-root, --project-id, and --mission-id"
        )
    return values  # type: ignore[return-value]


def _require_closed_mapping(
    value: Any,
    keys: set[str],
    *,
    location: str,
) -> Mapping[str, Any]:
    if not isinstance(value, Mapping) or set(value) != keys:
        raise MissionInterfaceError(
            "mission_host_bridge_request_invalid",
            f"{location} has the wrong closed shape.",
        )
    return value


def _require_exact_text(value: Any, *, location: str) -> str:
    if not isinstance(value, str) or not value.strip() or value != value.strip():
        raise ValueError(f"{location} must be exact non-empty text")
    return value


def _read_release_record_commit(record_path: Path) -> str:
    raw = _read_file(
        str(record_path),
        label="RH Mission release record",
    )
    try:
        record = loads_strict_json_object(raw)
    except (TypeError, ValueError, UnicodeError) as exc:
        raise ValueError(f"RH Mission release record is invalid: {exc}") from exc
    if record.get("schema_version") != MISSION_RELEASE_RECORD_SCHEMA_VERSION:
        raise ValueError("RH Mission release record schema is not current")
    release_sha = _require_exact_text(
        record.get("release_sha"), location="release_record.release_sha"
    )
    if _FULL_GIT_COMMIT.fullmatch(release_sha) is None:
        raise ValueError("release_record.release_sha must be one lowercase Git commit")
    return release_sha


def _read_release_commit(release_root: Path) -> str:
    return _read_release_record_commit(
        release_root / MISSION_RELEASE_RECORD_RELATIVE_PATH
    )


def _read_owner_release_commit() -> str:
    return _read_release_record_commit(MISSION_RELEASE_RECORD_PATH)


def _run_owner_genesis(
    arguments: argparse.Namespace,
    options: Mapping[str, str | None],
) -> Mapping[str, Any]:
    workspace_root, project_id, mission_id = _require_workspace_options(options)
    request = _read_request(arguments.request)
    request_keys = {"schema_version", "mission_id", "workspace_root", "source_commit"}
    if set(request) != request_keys:
        raise ValueError("owner genesis request has the wrong closed shape")
    if request["schema_version"] != MISSION_OWNER_GENESIS_REQUEST_SCHEMA_VERSION:
        raise ValueError("owner genesis request schema is not current")
    requested_mission_id = _require_exact_text(
        request["mission_id"], location="request.mission_id"
    )
    requested_workspace_root = _require_exact_text(
        request["workspace_root"], location="request.workspace_root"
    )
    source_commit = _require_exact_text(
        request["source_commit"], location="request.source_commit"
    )
    if requested_mission_id != mission_id:
        raise ValueError("owner genesis request differs from the Mission selector")
    requested_root = Path(requested_workspace_root)
    selected_root = Path(workspace_root)
    if not requested_root.is_absolute() or not selected_root.is_absolute():
        raise ValueError("owner genesis workspace root must be absolute")
    if requested_root.resolve() != selected_root.resolve():
        raise ValueError("owner genesis request differs from the workspace selector")
    if _FULL_GIT_COMMIT.fullmatch(source_commit) is None:
        raise ValueError("request.source_commit must be one lowercase Git commit")
    if _read_owner_release_commit() != source_commit:
        raise ValueError("owner genesis source commit differs from the installed release")

    canonical = load_canonical_snapshot(REPO_ROOT, source_commit=source_commit)
    if not canonical.ok or canonical.value is None:
        message = canonical.failure.message if canonical.failure is not None else "unknown failure"
        raise ValueError(f"canonical RH state could not be loaded: {message}")
    snapshot = canonical.value

    seed_raw = _read_file(
        str(REPO_ROOT / MISSION_SEED_RELATIVE_PATH),
        label="owner Mission seed",
    )
    try:
        seed = loads_strict_json_object(seed_raw)
    except (TypeError, ValueError, UnicodeError) as exc:
        raise ValueError(f"owner Mission seed is not strict UTF-8 JSON: {exc}") from exc
    try:
        genesis_seed = WorkspaceStore.validate_direct_mission_genesis_seed(
            seed,
            project_id=project_id,
            mission_id=mission_id,
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError(f"owner direct Mission seed is invalid: {exc}") from exc

    owner_command_id = f"owner-genesis:{source_commit}:{mission_id}"
    interface = MissionInterface.initialize_from_owner(
        selected_root,
        project_id=project_id,
        mission_id=mission_id,
        canonical_snapshot=snapshot,
        genesis_seed=genesis_seed,
        owner_command_id=owner_command_id,
    )
    return {
        "status": "initialized",
        "project_id": project_id,
        "mission_id": mission_id,
        "workspace_root": str(interface.workspace_root),
        "source_commit": source_commit,
    }


def _run_owner_model_migration(
    arguments: argparse.Namespace,
    options: Mapping[str, str | None],
) -> Mapping[str, Any]:
    workspace_root, project_id, mission_id = _require_workspace_options(options)
    request = _read_request(arguments.request)
    if set(request) != {
        "schema_version", "project_id", "mission_id", "workspace_root",
        "target_release_sha", "expected_mission_revision",
        "expected_mission_payload_sha256", "expected_canonical_authority_digest",
    }:
        raise ValueError("owner model migration request has the wrong closed shape")
    if request["schema_version"] != "mathematical_research.mission_model_policy_migration_request.v1":
        raise ValueError("owner model migration request schema is not current")
    for name, selected in (("project_id", project_id), ("mission_id", mission_id)):
        if _require_exact_text(request[name], location=f"request.{name}") != selected:
            raise ValueError(f"owner model migration differs from the {name} selector")
    requested_root = Path(_require_exact_text(request["workspace_root"], location="request.workspace_root"))
    selected_root = Path(workspace_root)
    if not requested_root.is_absolute() or not selected_root.is_absolute():
        raise ValueError("owner model migration workspace root must be absolute")
    if requested_root.resolve(strict=True) != selected_root.resolve(strict=True):
        raise ValueError("owner model migration differs from the workspace selector")
    release = _require_exact_text(request["target_release_sha"], location="request.target_release_sha")
    if _FULL_GIT_COMMIT.fullmatch(release) is None or _read_owner_release_commit() != release:
        raise ValueError("owner model migration target differs from the installed release")
    revision = request["expected_mission_revision"]
    if isinstance(revision, bool) or not isinstance(revision, int) or revision < 1:
        raise ValueError("owner model migration requires a positive exact Mission revision")
    for name in ("expected_mission_payload_sha256", "expected_canonical_authority_digest"):
        value = request[name]
        if not isinstance(value, str) or re.fullmatch(r"[0-9a-f]{64}", value) is None:
            raise ValueError(f"request.{name} must be one lowercase SHA-256")
    interface = MissionInterface.open(
        selected_root, expected_project_id=project_id, expected_mission_id=mission_id,
    )
    return interface.migrate_model_policy_from_owner(
        expected_mission_revision=revision,
        expected_mission_payload_sha256=request["expected_mission_payload_sha256"],
        expected_canonical_authority_digest=request["expected_canonical_authority_digest"],
    )


def _run_owner_scientific_context_revision(
    arguments: argparse.Namespace, options: Mapping[str, str | None],
) -> Mapping[str, Any]:
    workspace_root, project_id, mission_id = _require_workspace_options(options)
    request = _read_request(arguments.request)
    if set(request) != {
        "schema_version", "project_id", "mission_id", "workspace_root", "target_release_sha",
        "expected_mission_revision", "expected_mission_payload_sha256",
        "expected_canonical_authority_digest", "expected_store_cut", "update",
    } or request["schema_version"] != "mathematical_research.mission_scientific_context_revision_request.v1":
        raise ValueError("stopped scientific revision request has the wrong closed shape")
    root = Path(workspace_root)
    requested_root = Path(_require_exact_text(request["workspace_root"], location="request.workspace_root"))
    release = _require_exact_text(request["target_release_sha"], location="request.target_release_sha")
    if (request["project_id"] != project_id
        or request["mission_id"] != mission_id
        or not root.is_absolute() or not requested_root.is_absolute()
        or requested_root.resolve(strict=True) != root.resolve(strict=True)
        or _FULL_GIT_COMMIT.fullmatch(release) is None or _read_owner_release_commit() != release):
        raise ValueError("stopped scientific revision differs from its selected owner")
    revision = request["expected_mission_revision"]
    if isinstance(revision, bool) or not isinstance(revision, int) or revision < 1:
        raise ValueError("stopped scientific revision requires a positive exact Mission revision")
    for name in ("expected_mission_payload_sha256", "expected_canonical_authority_digest"):
        if not isinstance(request[name], str) or re.fullmatch(r"[0-9a-f]{64}", request[name]) is None:
            raise ValueError(f"request.{name} must be one lowercase SHA-256")
    update = request["update"]
    if (not isinstance(update, dict) or set(update) != {"context_id", "expected_head", "create", "patch"}
        or update["create"] is not None or not isinstance(update["expected_head"], dict)
        or not isinstance(update["patch"], dict)):
        raise ValueError("stopped scientific revision requires an exact existing Context patch")
    # The current Store owner performs CAS, reference and quiescent-custody
    # checks atomically. Do not fabricate an Epoch, reopen a prepared image,
    # run an audit or inspect newer heads before immutable replay selection.
    interface = MissionInterface.open(root, expected_project_id=project_id, expected_mission_id=mission_id)
    return interface.revise_scientific_context_from_owner(
        expected_mission_revision=revision,
        expected_mission_payload_sha256=request["expected_mission_payload_sha256"],
        expected_canonical_authority_digest=request["expected_canonical_authority_digest"],
        expected_store_cut=request["expected_store_cut"], update=update,
    )


def _run_owner_scientific_context_binding(
    arguments: argparse.Namespace,
    options: Mapping[str, str | None],
) -> Mapping[str, Any]:
    workspace_root, project_id, mission_id = _require_workspace_options(options)
    request = _read_request(arguments.request)
    if set(request) != {
        "schema_version", "project_id", "mission_id", "workspace_root",
        "target_release_sha", "expected_mission_revision",
        "expected_mission_payload_sha256", "expected_canonical_authority_digest",
        "context_id", "expected_context_revision", "expected_context_payload_sha256",
    }:
        raise ValueError("owner scientific Context binding request has the wrong closed shape")
    if request["schema_version"] != "mathematical_research.mission_scientific_context_binding_request.v1":
        raise ValueError("owner scientific Context binding request schema is not current")
    for name, selected in (("project_id", project_id), ("mission_id", mission_id)):
        if _require_exact_text(request[name], location=f"request.{name}") != selected:
            raise ValueError(f"owner scientific Context binding differs from the {name} selector")
    requested_root = Path(_require_exact_text(request["workspace_root"], location="request.workspace_root"))
    selected_root = Path(workspace_root)
    if not requested_root.is_absolute() or not selected_root.is_absolute():
        raise ValueError("owner scientific Context binding workspace root must be absolute")
    if requested_root.resolve(strict=True) != selected_root.resolve(strict=True):
        raise ValueError("owner scientific Context binding differs from the workspace selector")
    release = _require_exact_text(request["target_release_sha"], location="request.target_release_sha")
    if _FULL_GIT_COMMIT.fullmatch(release) is None or _read_owner_release_commit() != release:
        raise ValueError("owner scientific Context binding target differs from the installed release")
    for name in ("expected_mission_revision", "expected_context_revision"):
        revision = request[name]
        if isinstance(revision, bool) or not isinstance(revision, int) or revision < 1:
            raise ValueError(f"request.{name} must be one positive exact revision")
    for name in (
        "expected_mission_payload_sha256", "expected_context_payload_sha256",
        "expected_canonical_authority_digest",
    ):
        digest = request[name]
        if not isinstance(digest, str) or re.fullmatch(r"[0-9a-f]{64}", digest) is None:
            raise ValueError(f"request.{name} must be one lowercase SHA-256")
    context_id = TypedWorkspaceId(IdentityKind.CONTEXT, request["context_id"]).value
    if context_id.startswith("context:"):
        raise ValueError("owner scientific Context binding requires a bare Context identity")
    interface = MissionInterface.open(
        selected_root, expected_project_id=project_id, expected_mission_id=mission_id,
    )
    return interface.bind_scientific_context_from_owner(
        expected_mission_revision=request["expected_mission_revision"],
        expected_mission_payload_sha256=request["expected_mission_payload_sha256"],
        expected_canonical_authority_digest=request["expected_canonical_authority_digest"],
        context_id=context_id,
        expected_context_revision=request["expected_context_revision"],
        expected_context_payload_sha256=request["expected_context_payload_sha256"],
    )


def _run_owner_mission_reauthorization(
    arguments: argparse.Namespace,
    options: Mapping[str, str | None],
) -> Mapping[str, Any]:
    workspace_root, project_id, mission_id = _require_workspace_options(options)
    request = _read_request(arguments.request)
    if set(request) != {
        "schema_version", "project_id", "mission_id", "workspace_root",
        "target_release_sha", "expected_mission_revision",
        "expected_mission_payload_sha256", "executive_epoch_id", "root_thread_id",
        "expected_terminal_event_sha256", "expected_canonical_authority_digest",
    }:
        raise ValueError("owner Mission reauthorization request has the wrong closed shape")
    if request["schema_version"] != "mathematical_research.mission_reauthorization_request.v1":
        raise ValueError("owner Mission reauthorization request schema is not current")
    for name, selected in (("project_id", project_id), ("mission_id", mission_id)):
        if _require_exact_text(request[name], location=f"request.{name}") != selected:
            raise ValueError(f"owner Mission reauthorization differs from the {name} selector")
    requested_root = Path(_require_exact_text(request["workspace_root"], location="request.workspace_root"))
    selected_root = Path(workspace_root)
    if not requested_root.is_absolute() or not selected_root.is_absolute():
        raise ValueError("owner Mission reauthorization workspace root must be absolute")
    if requested_root.resolve(strict=True) != selected_root.resolve(strict=True):
        raise ValueError("owner Mission reauthorization differs from the workspace selector")
    release = _require_exact_text(request["target_release_sha"], location="request.target_release_sha")
    if _FULL_GIT_COMMIT.fullmatch(release) is None or _read_owner_release_commit() != release:
        raise ValueError("owner Mission reauthorization target differs from the installed release")
    revision = request["expected_mission_revision"]
    if isinstance(revision, bool) or not isinstance(revision, int) or revision < 1:
        raise ValueError("owner Mission reauthorization requires a positive exact Mission revision")
    for name in (
        "expected_mission_payload_sha256", "expected_terminal_event_sha256",
        "expected_canonical_authority_digest",
    ):
        value = request[name]
        if not isinstance(value, str) or re.fullmatch(r"[0-9a-f]{64}", value) is None:
            raise ValueError(f"request.{name} must be one lowercase SHA-256")
    for name in ("executive_epoch_id", "root_thread_id"):
        _require_exact_text(request[name], location=f"request.{name}")
    interface = MissionInterface.open(
        selected_root, expected_project_id=project_id, expected_mission_id=mission_id,
    )
    return interface.reauthorize_mission_from_owner(
        expected_mission_revision=revision,
        expected_mission_payload_sha256=request["expected_mission_payload_sha256"],
        executive_epoch_id=request["executive_epoch_id"],
        root_thread_id=request["root_thread_id"],
        expected_terminal_event_sha256=request["expected_terminal_event_sha256"],
        expected_canonical_authority_digest=request["expected_canonical_authority_digest"],
    )


def _require_host_text(value: Any, *, location: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise MissionInterfaceError(
            "mission_host_bridge_request_invalid",
            f"{location} must be a non-empty string.",
        )
    return value


def _validate_host_store_cut(value: Any, *, location: str) -> Mapping[str, Any]:
    cut = _require_closed_mapping(
        value,
        {
            "project_commit",
            "current_root_digest",
            "transition_head_digest",
            "canonical_authority_digest",
        },
        location=location,
    )
    project_commit = cut["project_commit"]
    if (
        isinstance(project_commit, bool)
        or not isinstance(project_commit, int)
        or project_commit < 0
    ):
        raise MissionInterfaceError(
            "mission_host_bridge_request_invalid",
            f"{location}.project_commit must be a nonnegative integer.",
        )
    for key in ("current_root_digest", "canonical_authority_digest"):
        digest = cut[key]
        if not isinstance(digest, str) or _SHA256.fullmatch(digest) is None:
            raise MissionInterfaceError(
                "mission_host_bridge_request_invalid",
                f"{location}.{key} must be one lowercase SHA-256 digest.",
            )
    transition = cut["transition_head_digest"]
    if transition is not None and (
        not isinstance(transition, str) or _SHA256.fullmatch(transition) is None
    ):
        raise MissionInterfaceError(
            "mission_host_bridge_request_invalid",
            f"{location}.transition_head_digest must be null or one lowercase SHA-256 digest.",
        )
    return deep_thaw(cut)


def _validate_host_binding(
    binding: Any,
    *,
    action: str,
) -> Mapping[str, Any]:
    item = _HOST_BRIDGE_ACTIONS[action]
    keys = item.get("binding_keys")
    if not isinstance(keys, set):
        raise MissionInterfaceError(
            "mission_host_bridge_request_invalid",
            f"{action} does not accept a nested causal binding.",
        )
    value = _require_closed_mapping(binding, keys, location=f"payload.{action}.binding")
    if action in {
        "execute_semantic_operation",
        "execute_formal_attempt",
        "open_complete_claim_admission_case",
    }:
        for key in ("rootThreadId", "executiveEpochId"):
            _require_host_text(value[key], location=f"payload.binding.{key}")
    elif action in {
        "issue_historical_read_grant",
        "issue_research_read_grant",
        "issue_candidate_a1_review_grant",
        "issue_admission_review_grant",
        "issue_admission_decision_grant",
    }:
        for key in (
            "rootThreadId",
            "executiveEpochId",
            "childThreadId",
            "parentThreadId",
        ):
            _require_host_text(value[key], location=f"payload.binding.{key}")
        if value["depth"] != 1:
            raise MissionInterfaceError(
                "mission_host_bridge_binding_mismatch",
                "Delegated grants require one authenticated direct child.",
            )
    elif action in {
        "execute_delegated_read",
        "execute_research_read",
        "execute_candidate_a1_review",
        "execute_admission_review",
        "execute_admission_decision",
    }:
        for key in (
            "rootThreadId",
            "executiveEpochId",
            "callerThreadId",
            "parentThreadId",
            "turnId",
        ):
            _require_host_text(value[key], location=f"payload.binding.{key}")
        if action in {"execute_delegated_read", "execute_research_read"}:
            _require_host_text(
                value["grantId"], location="payload.binding.grantId"
            )
        if action == "execute_research_read":
            _require_host_text(value["assignmentId"], location="payload.binding.assignmentId")
        if value["depth"] != 1:
            raise MissionInterfaceError(
                "mission_host_bridge_binding_mismatch",
                "Delegated specialist calls require one authenticated direct child.",
            )
    elif action == "capture_native_material_observation":
        for key in ("rootThreadId", "executiveEpochId"):
            _require_host_text(value[key], location=f"payload.binding.{key}")
    else:
        _require_host_text(
            value["executiveEpochId"],
            location="payload.binding.executiveEpochId",
        )
    return value


def _current_epoch_binding(
    interface: Any,
    *,
    allowed_states: frozenset[str] = frozenset({"open"}),
) -> Mapping[str, Any]:
    method = getattr(interface, "current_epoch", None)
    if not callable(method):
        raise MissionInterfaceError(
            "mission_operation_unavailable",
            "MissionInterface.current_epoch is unavailable for Host binding validation.",
            missing_owner_primitives=("current_epoch",),
        )
    current = method()
    if (
        not isinstance(current, Mapping)
        or current.get("state") not in allowed_states
    ):
        raise MissionInterfaceError(
            "mission_host_bridge_binding_mismatch",
            "The Host action is not bound to an allowed current executive epoch.",
        )
    entry = current.get("entry")
    if not isinstance(entry, Mapping):
        raise MissionInterfaceError(
            "mission_host_bridge_binding_mismatch",
            "The current executive epoch has no owner entry projection.",
        )
    executive_epoch_id = entry.get("executive_epoch_id")
    if not isinstance(executive_epoch_id, str) or not executive_epoch_id:
        raise MissionInterfaceError(
            "mission_host_bridge_binding_mismatch",
            "The current executive epoch has no owner identity.",
        )
    root_thread_id = entry.get("goal_thread_id")
    if root_thread_id is not None and (
        not isinstance(root_thread_id, str) or not root_thread_id
    ):
        raise MissionInterfaceError(
            "mission_host_bridge_binding_mismatch",
            "The current executive epoch has an invalid Goal thread binding.",
        )
    workspace_root = entry.get("workspace_root")
    if workspace_root is not None and (
        not isinstance(workspace_root, str) or not workspace_root
    ):
        raise MissionInterfaceError(
            "mission_host_bridge_binding_mismatch",
            "The current executive epoch has an invalid workspace binding.",
        )
    return {
        "state": current["state"],
        "rootThreadId": root_thread_id,
        "executiveEpochId": executive_epoch_id,
        "workspaceRoot": workspace_root,
    }


def _validate_current_epoch_causality(
    interface: Any,
    *,
    action: str,
    binding: Mapping[str, Any],
    semantic_operation: str | None = None,
) -> None:
    if action == "bind_executive_epoch":
        allowed_states = frozenset({"authorized", "open"})
    elif action == "record_direct_failed_executive_epoch":
        allowed_states = frozenset(
            {"authorized", "open", "closed", "mission_fenced"}
        )
    elif action == "execute_semantic_operation" and semantic_operation == "checkpoint":
        allowed_states = frozenset({"open", "closed", "mission_fenced"})
    elif action == "fence_mission":
        allowed_states = frozenset({"open", "mission_fenced"})
    else:
        allowed_states = frozenset({"open"})
    current = _current_epoch_binding(interface, allowed_states=allowed_states)
    if (
        "rootThreadId" in binding
        and current["rootThreadId"] is not None
        and binding["rootThreadId"] != current["rootThreadId"]
    ):
        raise MissionInterfaceError(
            "mission_host_bridge_binding_mismatch",
            "Host root thread differs from the current owner epoch.",
        )
    if (
        "executiveEpochId" in binding
        and binding["executiveEpochId"] != current["executiveEpochId"]
    ):
        raise MissionInterfaceError(
            "mission_host_bridge_binding_mismatch",
            "Host epoch identity differs from the current owner epoch.",
        )
    if (
        "workspaceRoot" in binding
        and current["workspaceRoot"] is not None
        and binding["workspaceRoot"] != current["workspaceRoot"]
    ):
        raise MissionInterfaceError(
            "mission_host_bridge_binding_mismatch",
            "Host workspace differs from the current owner epoch.",
        )


def _validate_host_observation(
    value: Any,
    *,
    binding: Mapping[str, Any],
) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise MissionInterfaceError(
            "mission_host_bridge_request_invalid",
            "payload.observation must be an object.",
        )
    if value.get("rootThreadId") != binding["rootThreadId"]:
        raise MissionInterfaceError(
            "mission_host_bridge_binding_mismatch",
            "native observation root differs from its Host causal binding.",
        )
    return value


def _validate_preview_result(
    interface: Any,
    result: Mapping[str, Any],
) -> Mapping[str, Any]:
    schema_method = getattr(interface, "semantic_preview_schema", None)
    if not callable(schema_method):
        schema_method = getattr(type(interface), "semantic_preview_schema", None)
    if not callable(schema_method):
        raise MissionInterfaceError(
            "mission_operation_unavailable",
            "MissionInterface.semantic_preview_schema is unavailable.",
            missing_owner_primitives=("semantic_preview_schema",),
        )
    schema = deep_thaw(schema_method())
    normalized = deep_thaw(result)
    violations = _schema_violations(normalized, schema, schema, "$")
    if violations:
        location, message = violations[0]
        raise MissionInterfaceError(
            "mission_operation_result_invalid",
            f"Semantic preview differs from its closed owner schema at {location}: {message}",
        )
    return normalized


def _validate_formal_attempt_payload(payload: Mapping[str, Any]) -> dict[str, Any]:
    request = _require_closed_mapping(
        payload["request"],
        _FORMAL_ATTEMPT_REQUEST_KEYS,
        location="payload.request",
    )
    selected_bet_sha256 = _require_host_text(
        request["selected_bet_sha256"],
        location="payload.request.selected_bet_sha256",
    )
    if _SHA256.fullmatch(selected_bet_sha256) is None:
        raise MissionInterfaceError(
            "mission_host_bridge_request_invalid",
            "payload.request.selected_bet_sha256 must be one lowercase SHA-256 digest.",
        )
    correction_basis = request["correction_basis"]
    if correction_basis is not None:
        correction_basis = _require_host_text(
            correction_basis,
            location="payload.request.correction_basis",
        )

    runtime = _require_closed_mapping(
        payload["runtime"],
        _FORMAL_ATTEMPT_RUNTIME_KEYS,
        location="payload.runtime",
    )
    for key in (
        "release_root",
        "state_root",
        "codex_executable",
        "codex_home",
        "outer_containment_id",
    ):
        _require_host_text(runtime[key], location=f"payload.runtime.{key}")
    release_commit = _require_host_text(
        runtime["release_commit"],
        location="payload.runtime.release_commit",
    )
    if _FULL_GIT_COMMIT.fullmatch(release_commit) is None:
        raise MissionInterfaceError(
            "mission_host_bridge_request_invalid",
            "payload.runtime.release_commit must be one exact lowercase Git commit.",
        )
    for key in ("trusted_mcp_server_ids", "trusted_app_ids"):
        values = runtime[key]
        if not isinstance(values, list):
            raise MissionInterfaceError(
                "mission_host_bridge_request_invalid",
                f"payload.runtime.{key} must be an exact JSON array.",
            )
        normalized = [
            _require_host_text(item, location=f"payload.runtime.{key}[{index}]")
            for index, item in enumerate(values)
        ]
        if len(normalized) != len(set(normalized)):
            raise MissionInterfaceError(
                "mission_host_bridge_request_invalid",
                f"payload.runtime.{key} contains a duplicate trusted connector.",
            )
    cadence = runtime["polling_cadence_seconds"]
    if (
        isinstance(cadence, bool)
        or not isinstance(cadence, (int, float))
        or cadence <= 0
    ):
        raise MissionInterfaceError(
            "mission_host_bridge_request_invalid",
            "payload.runtime.polling_cadence_seconds must be a positive transport cadence.",
        )
    return {
        "request": {
            "selected_bet_sha256": selected_bet_sha256,
            "correction_basis": correction_basis,
        },
        "binding": deep_thaw(payload["binding"]),
        "runtime": deep_thaw(runtime),
    }


def _parse_host_bridge_request(
    value: Mapping[str, Any],
    *,
    project_id: str,
    mission_id: str,
) -> tuple[str, Mapping[str, Any]]:
    request = _require_closed_mapping(value, _HOST_BRIDGE_KEYS, location="request")
    if request["schema_version"] != MISSION_HOST_BRIDGE_REQUEST_SCHEMA_VERSION:
        raise MissionInterfaceError(
            "mission_host_bridge_request_invalid",
            "Host bridge request schema version is unsupported.",
        )
    action = _require_host_text(request["action"], location="request.action")
    if action not in _HOST_BRIDGE_ACTIONS:
        raise MissionInterfaceError(
            "mission_host_bridge_request_invalid",
            f"Unsupported Mission Host bridge action: {action!r}.",
        )
    binding = _require_closed_mapping(
        request["binding"],
        _HOST_BRIDGE_OUTER_BINDING_KEYS,
        location="request.binding",
    )
    if binding["project_id"] != project_id or binding["mission_id"] != mission_id:
        raise MissionInterfaceError(
            "mission_host_bridge_binding_mismatch",
            "Host bridge selectors differ from the exact request binding.",
        )
    raw_payload = request["payload"]
    item = _HOST_BRIDGE_ACTIONS[action]
    required = item["payload_keys"]
    optional = item.get("optional_payload_keys", set())
    if (
        not isinstance(raw_payload, Mapping)
        or not required.issubset(raw_payload)
        or set(raw_payload) - required - optional
    ):
        raise MissionInterfaceError(
            "mission_host_bridge_request_invalid",
            f"payload for {action} has the wrong closed shape.",
        )
    payload = deep_thaw(raw_payload)
    if action == "authorize_executive_epoch":
        payload["expected_cut"] = _validate_host_store_cut(
            payload["expected_cut"], location="payload.expected_cut"
        )
    elif action == "validate_suspended_epoch_cut":
        payload["expected_cut"] = _validate_host_store_cut(
            payload["expected_cut"], location="payload.expected_cut"
        )
        for key in ("executiveEpochId", "rootThreadId", "workspaceRoot"):
            _require_host_text(payload[key], location=f"payload.{key}")
    elif action == "bind_executive_epoch":
        for key in ("executiveEpochId", "rootThreadId", "workspaceRoot"):
            _require_host_text(payload[key], location=f"payload.{key}")
    elif action == "record_direct_failed_executive_epoch":
        _require_host_text(
            payload["executiveEpochId"], location="payload.executiveEpochId"
        )
        reconciliation = payload["reconciliation"]
        if (
            not isinstance(reconciliation, Mapping)
            or set(reconciliation) != {"stage", "failure_reason"}
            or reconciliation["stage"] not in {"authorization_only", "goal_runtime"}
        ):
            raise MissionInterfaceError(
                "mission_host_bridge_request_invalid",
                "payload.reconciliation must contain only the factual failure stage and reason.",
            )
        _require_host_text(
            reconciliation["failure_reason"],
            location="payload.reconciliation.failure_reason",
        )
    elif action == "execute_semantic_operation":
        causal = _validate_host_binding(
            payload["binding"], action=action
        )
        payload["request"] = deep_thaw(validate_semantic_request(payload["request"]))
        payload["binding"] = deep_thaw(causal)
        if "root_query_context" in payload:
            query_context = payload["root_query_context"]
            if (
                not isinstance(query_context, Mapping)
                or set(query_context) != {"cursor_mac_key"}
                or not isinstance(query_context["cursor_mac_key"], str)
                or _SHA256.fullmatch(query_context["cursor_mac_key"]) is None
            ):
                raise MissionInterfaceError(
                    "mission_host_bridge_request_invalid",
                    "Private root query context has an invalid closed shape.",
                )
            payload["root_query_context"] = deep_thaw(query_context)
    elif action == "open_complete_claim_admission_case":
        causal = _validate_host_binding(payload["binding"], action=action)
        payload["request"] = deep_thaw(
            validate_admission_case_request(payload["request"])
        )
        payload["binding"] = deep_thaw(causal)
    elif action in {"issue_historical_read_grant", "issue_research_read_grant"}:
        causal = _validate_host_binding(payload["binding"], action=action)
        validator = (validate_research_read_grant_request if action == "issue_research_read_grant"
                     else validate_historical_read_grant_request)
        payload["request"] = deep_thaw(
            validator(payload["request"])
        )
        if payload["request"]["child_thread_id"] != causal["childThreadId"]:
            raise MissionInterfaceError(
                "mission_host_bridge_binding_mismatch",
                "Read grant target differs from the authenticated direct child.",
            )
        if causal["parentThreadId"] != causal["rootThreadId"]:
            raise MissionInterfaceError(
                "mission_host_bridge_binding_mismatch",
                "Read grant target is not a direct child of the root executive.",
            )
        payload["binding"] = deep_thaw(causal)
    elif action == "issue_candidate_a1_review_grant":
        causal = _validate_host_binding(payload["binding"], action=action)
        payload["request"] = deep_thaw(
            validate_candidate_a1_review_grant_request(payload["request"])
        )
        if payload["request"]["child_thread_id"] != causal["childThreadId"]:
            raise MissionInterfaceError(
                "mission_host_bridge_binding_mismatch",
                "Candidate A1 review grant target differs from the authenticated child.",
            )
        if causal["parentThreadId"] != causal["rootThreadId"]:
            raise MissionInterfaceError(
                "mission_host_bridge_binding_mismatch",
                "Candidate A1 reviewer is not a direct child of the root executive.",
            )
        payload["binding"] = deep_thaw(causal)
    elif action in {
        "issue_admission_review_grant",
        "issue_admission_decision_grant",
    }:
        causal = _validate_host_binding(payload["binding"], action=action)
        validator = (
            validate_admission_review_grant_request
            if action == "issue_admission_review_grant"
            else validate_admission_decision_grant_request
        )
        payload["request"] = deep_thaw(validator(payload["request"]))
        if payload["request"]["child_thread_id"] != causal["childThreadId"]:
            raise MissionInterfaceError(
                "mission_host_bridge_binding_mismatch",
                "Admission grant target differs from the authenticated child.",
            )
        if causal["parentThreadId"] != causal["rootThreadId"]:
            raise MissionInterfaceError(
                "mission_host_bridge_binding_mismatch",
                "Admission actor is not a direct child of the root executive.",
            )
        payload["binding"] = deep_thaw(causal)
    elif action in {"execute_delegated_read", "execute_research_read"}:
        causal = _validate_host_binding(payload["binding"], action=action)
        if causal["parentThreadId"] != causal["rootThreadId"]:
            raise MissionInterfaceError(
                "mission_host_bridge_binding_mismatch",
                "Read caller is not a direct child of the root executive.",
            )
        validator = (validate_research_read_request if action == "execute_research_read"
                     else validate_historical_read_request)
        payload["request"] = deep_thaw(
            validator(payload["request"])
        )
        if not isinstance(payload["grant"], Mapping):
            raise MissionInterfaceError(
                "mission_host_bridge_request_invalid",
                "payload.grant must be the exact owner-issued grant envelope.",
            )
        payload["grant"] = deep_thaw(payload["grant"])
        payload["binding"] = deep_thaw(causal)
        if action == "execute_research_read":
            query_context = payload["research_query_context"]
            if (not isinstance(query_context, Mapping)
                or set(query_context) != {"cursor_mac_key"}
                or not isinstance(query_context["cursor_mac_key"], str)
                or _SHA256.fullmatch(query_context["cursor_mac_key"]) is None):
                raise MissionInterfaceError("mission_host_bridge_request_invalid", "Private research query context has an invalid closed shape.")
            payload["research_query_context"] = deep_thaw(query_context)
    elif action == "execute_candidate_a1_review":
        causal = _validate_host_binding(payload["binding"], action=action)
        if causal["parentThreadId"] != causal["rootThreadId"]:
            raise MissionInterfaceError(
                "mission_host_bridge_binding_mismatch",
                "Candidate A1 reviewer is not a direct child of the root executive.",
            )
        payload["request"] = deep_thaw(
            validate_candidate_a1_review_request(payload["request"])
        )
        if not isinstance(payload["grant"], Mapping):
            raise MissionInterfaceError(
                "mission_host_bridge_request_invalid",
                "payload.grant must be the exact Candidate A1 review grant.",
            )
        payload["grant"] = deep_thaw(payload["grant"])
        payload["binding"] = deep_thaw(causal)
    elif action in {"execute_admission_review", "execute_admission_decision"}:
        causal = _validate_host_binding(payload["binding"], action=action)
        if causal["parentThreadId"] != causal["rootThreadId"]:
            raise MissionInterfaceError(
                "mission_host_bridge_binding_mismatch",
                "Admission caller is not a direct child of the root executive.",
            )
        validator = (
            validate_admission_review_request
            if action == "execute_admission_review"
            else validate_admission_decision_request
        )
        payload["request"] = deep_thaw(validator(payload["request"]))
        if not isinstance(payload["grant"], Mapping):
            raise MissionInterfaceError(
                "mission_host_bridge_request_invalid",
                "payload.grant must be the exact Admission grant.",
            )
        payload["grant"] = deep_thaw(payload["grant"])
        payload["binding"] = deep_thaw(causal)
    elif action == "observe_mission":
        if (
            not isinstance(payload["cursor_mac_key"], str)
            or _SHA256.fullmatch(payload["cursor_mac_key"]) is None
        ):
            raise MissionInterfaceError(
                "mission_host_bridge_request_invalid",
                "Private observation cursor context is invalid.",
            )
        try:
            payload["request"] = validate_observation_request(payload["request"])
        except (ValueError, TypeError) as exc:
            raise MissionInterfaceError(
                "mission_host_bridge_request_invalid",
                "Private observation request is invalid.",
            ) from exc
    elif action == "reconstruct" and "selected_readback_handles" in payload:
        handles = payload["selected_readback_handles"]
        if (
            not isinstance(handles, list)
            or any(
                not isinstance(handle, str)
                or not handle.strip()
                or handle != handle.strip()
                for handle in handles
            )
            or len(handles) != len(set(handles))
        ):
            raise MissionInterfaceError(
                "mission_host_bridge_request_invalid",
                "reconstruct selected_readback_handles must be unique exact strings.",
            )
    elif action == "execute_formal_attempt":
        causal = _validate_host_binding(payload["binding"], action=action)
        payload["binding"] = deep_thaw(causal)
        payload = _validate_formal_attempt_payload(payload)
    elif action == "capture_native_material_observation":
        causal = _validate_host_binding(
            payload["binding"], action=action
        )
        payload["observation"] = deep_thaw(
            _validate_host_observation(payload["observation"], binding=causal)
        )
        payload["binding"] = deep_thaw(causal)
    elif action in {
        "fence_mission",
    }:
        causal = _validate_host_binding(
            payload["binding"], action=action
        )
        if not isinstance(payload["context"], Mapping):
            raise MissionInterfaceError(
                "mission_host_bridge_request_invalid",
                f"payload.{action}.context must be an object.",
            )
        payload["binding"] = deep_thaw(causal)
    if action in {"host_snapshot", "observe_mission"} and "canonical_source" in payload:
        source = payload["canonical_source"]
        if (
            not isinstance(source, Mapping)
            or set(source) != {"repo_root", "release_sha"}
            or not isinstance(source["repo_root"], str)
            or not source["repo_root"]
            or not Path(source["repo_root"]).is_absolute()
            or not isinstance(source["release_sha"], str)
            or _FULL_GIT_COMMIT.fullmatch(source["release_sha"]) is None
            or Path(source["repo_root"]).name != source["release_sha"]
            or Path(source["repo_root"]) != Path(source["repo_root"]).resolve()
        ):
            raise MissionInterfaceError(
                "mission_host_bridge_request_invalid",
                "Private canonical byte source requires one exact selected release root and identity.",
            )
        payload["canonical_source"] = deep_thaw(source)
    return action, payload


def _dispatch_host_bridge_attempt(
    *,
    workspace_root: str,
    project_id: str,
    mission_id: str,
    action: str,
    payload: Mapping[str, Any],
    observer: CaptureWorkspaceObserver | None = None,
) -> Mapping[str, Any]:
    """Run one complete owner operation against a freshly opened facade."""

    attempt_payload = deep_thaw(payload)
    if action in {"host_snapshot", "observe_mission"} and "canonical_source" in attempt_payload:
        source = attempt_payload["canonical_source"]
        interface = MissionInterface.open(
            workspace_root, project_id, mission_id,
            canonical_repo_root=source["repo_root"],
        )
    else:
        with mission_fence_diagnostic_stage(
            "owner_open" if action == "fence_mission" else None
        ):
            interface = MissionInterface.open(
                workspace_root,
                project_id,
                mission_id,
                **(
                    {
                        "observer": observer,
                        "observation_environment": ObservationEnvironment.PROD,
                    }
                    if observer is not None
                    else {}
                ),
            )
    binding = attempt_payload.get("binding")
    if isinstance(binding, Mapping) and action != "capture_native_material_observation":
        with mission_fence_diagnostic_stage(
            "host_binding" if action == "fence_mission" else None
        ):
            _validate_current_epoch_causality(
                interface,
                action=action,
                binding=binding,
                semantic_operation=(
                    attempt_payload["request"]["operation"]
                    if action == "execute_semantic_operation"
                    else None
                ),
            )
    elif action in {
        "bind_executive_epoch",
        "record_direct_failed_executive_epoch",
    }:
        _validate_current_epoch_causality(
            interface, action=action, binding=attempt_payload
        )
    if action == "execute_formal_attempt":
        assert isinstance(binding, Mapping)
        authority = interface._direct_epoch_authority(  # noqa: SLF001
            str(binding["executiveEpochId"])
        )
        if authority is None:
            raise MissionInterfaceError(
                "mission_host_bridge_binding_mismatch",
                "Formal execution requires the exact active direct Executive Epoch.",
            )
        runtime_payload = attempt_payload["runtime"]
        runtime = FormalAttemptRuntimeFacts(
            release_root=Path(runtime_payload["release_root"]),
            release_commit=str(runtime_payload["release_commit"]),
            state_root=Path(runtime_payload["state_root"]),
            codex_executable=Path(runtime_payload["codex_executable"]),
            codex_home=Path(runtime_payload["codex_home"]),
            outer_containment_id=str(runtime_payload["outer_containment_id"]),
            trusted_mcp_server_ids=tuple(runtime_payload["trusted_mcp_server_ids"]),
            trusted_app_ids=tuple(runtime_payload["trusted_app_ids"]),
            polling_cadence_seconds=float(
                runtime_payload["polling_cadence_seconds"]
            ),
        )
        control = FormalAttemptControl()
        previous_handlers: dict[int, Any] = {}

        def handle_control(signum: int, _frame: Any) -> None:
            if signum == getattr(signal, "SIGUSR2", None):
                control.request_force_stop()
            else:
                control.request_cancel()

        control_signals = [signal.SIGINT, signal.SIGTERM]
        explicit_force_signal = getattr(signal, "SIGUSR2", None)
        if explicit_force_signal is not None:
            control_signals.append(explicit_force_signal)
        try:
            adapter = build_formal_attempt_adapter(  # noqa: SLF001
                cas=interface._cas,
                runtime=runtime,
            )
            for signum in control_signals:
                previous_handlers[int(signum)] = signal.getsignal(signum)
                signal.signal(signum, handle_control)
            request = attempt_payload["request"]
            result = execute_formal_attempt_operation(
                interface._store,  # noqa: SLF001
                cas=interface._cas,  # noqa: SLF001
                authority=authority,
                adapter=adapter,
                runtime=runtime,
                selected_bet_sha256=str(request["selected_bet_sha256"]),
                correction_basis=request["correction_basis"],
                lease=interface._writer_lease(),  # noqa: SLF001
                control=control,
            )
        except (FormalAttemptBridgeError, ResearchAttemptAdapterError) as exc:
            raise MissionInterfaceError(exc.code, str(exc)) from exc
        finally:
            for signum, handler in previous_handlers.items():
                signal.signal(signum, handler)
        return result.to_payload()

    method_name = str(_HOST_BRIDGE_ACTIONS[action]["owner_method"])
    method = getattr(interface, method_name, None)
    if not callable(method):
        raise MissionInterfaceError(
            "mission_operation_unavailable",
            f"MissionInterface.{method_name} is unavailable.",
            missing_owner_primitives=(method_name,),
        )
    if action == "execute_semantic_operation":
        owner_options: dict[str, Any] = {
            "executive_epoch_id": binding["executiveEpochId"],
        }
        if "root_query_context" in attempt_payload:
            owner_options["root_query_context"] = attempt_payload["root_query_context"]
        result = method(attempt_payload["request"], **owner_options)
    elif action == "open_complete_claim_admission_case":
        result = method(
            attempt_payload["request"],
            executive_epoch_id=binding["executiveEpochId"],
        )
    elif action in {"issue_historical_read_grant", "issue_research_read_grant"}:
        owner_binding = {
            "project_id": project_id,
            "mission_id": mission_id,
            "executive_epoch_id": binding["executiveEpochId"],
            "root_thread_id": binding["rootThreadId"],
            "actual_child_thread_id": binding["childThreadId"],
            "actual_parent_thread_id": binding["parentThreadId"],
            "actual_depth": binding["depth"],
        }
        result = method(
            attempt_payload["request"],
            binding=owner_binding,
        )
    elif action == "issue_candidate_a1_review_grant":
        owner_binding = {
            "project_id": project_id,
            "mission_id": mission_id,
            "executive_epoch_id": binding["executiveEpochId"],
            "root_thread_id": binding["rootThreadId"],
            "actual_child_thread_id": binding["childThreadId"],
            "actual_parent_thread_id": binding["parentThreadId"],
            "actual_depth": binding["depth"],
        }
        result = method(
            attempt_payload["request"],
            binding=owner_binding,
        )
    elif action in {
        "issue_admission_review_grant",
        "issue_admission_decision_grant",
    }:
        owner_binding = {
            "project_id": project_id,
            "mission_id": mission_id,
            "executive_epoch_id": binding["executiveEpochId"],
            "root_thread_id": binding["rootThreadId"],
            "actual_child_thread_id": binding["childThreadId"],
            "actual_parent_thread_id": binding["parentThreadId"],
            "actual_depth": binding["depth"],
        }
        result = method(
            attempt_payload["request"],
            binding=owner_binding,
        )
    elif action in {"execute_delegated_read", "execute_research_read"}:
        owner_binding = {
            "project_id": project_id,
            "mission_id": mission_id,
            "executive_epoch_id": binding["executiveEpochId"],
            "root_thread_id": binding["rootThreadId"],
            "actual_child_thread_id": binding["callerThreadId"],
            "actual_parent_thread_id": binding["parentThreadId"],
            "actual_depth": binding["depth"],
            "expected_grant_id": binding["grantId"],
        }
        options = {}
        if action == "execute_research_read":
            owner_binding["expected_assignment_id"] = binding["assignmentId"]
            options["research_query_context"] = attempt_payload["research_query_context"]
        result = method(
            attempt_payload["request"],
            grant=attempt_payload["grant"],
            binding=owner_binding,
            **options,
        )
    elif action == "execute_candidate_a1_review":
        owner_binding = {
            "project_id": project_id,
            "mission_id": mission_id,
            "executive_epoch_id": binding["executiveEpochId"],
            "root_thread_id": binding["rootThreadId"],
            "actual_child_thread_id": binding["callerThreadId"],
            "actual_parent_thread_id": binding["parentThreadId"],
            "actual_depth": binding["depth"],
        }
        result = method(
            attempt_payload["request"],
            grant=attempt_payload["grant"],
            binding=owner_binding,
        )
    elif action in {"execute_admission_review", "execute_admission_decision"}:
        owner_binding = {
            "project_id": project_id,
            "mission_id": mission_id,
            "executive_epoch_id": binding["executiveEpochId"],
            "root_thread_id": binding["rootThreadId"],
            "actual_child_thread_id": binding["callerThreadId"],
            "actual_parent_thread_id": binding["parentThreadId"],
            "actual_depth": binding["depth"],
        }
        result = method(
            attempt_payload["request"],
            grant=attempt_payload["grant"],
            binding=owner_binding,
        )
    elif action == "capture_native_material_observation":
        try:
            result = method(
                attempt_payload["observation"],
                executive_epoch_id=binding["executiveEpochId"],
            )
        except (WorkspaceIntegrityError, IntegrityFreeze) as exc:
            raise MissionInterfaceError(
                "mission_shared_integrity_failure",
                "Native material capture detected shared Store/CAS integrity loss.",
            ) from exc
    elif action == "fence_mission":
        with mission_fence_diagnostic_stage("owner"):
            result = method(
                attempt_payload["context"],
                executive_epoch_id=binding["executiveEpochId"],
            )
    elif action == "observe_mission":
        result = method(
            attempt_payload["request"],
            cursor_mac_key=attempt_payload["cursor_mac_key"],
        )
    elif action in {"reconstruct", "host_snapshot"}:
        if "selected_readback_handles" in attempt_payload:
            result = method(
                selected_readback_handles=attempt_payload[
                    "selected_readback_handles"
                ]
            )
        else:
            result = method()
    else:
        result = method(attempt_payload)
    if not isinstance(result, Mapping):
        raise MissionInterfaceError(
            "mission_host_bridge_result_invalid",
            f"MissionInterface.{method_name} returned no result object.",
        )
    if action in {"execute_semantic_operation", "execute_delegated_read"}:
        return deep_thaw(
            validate_semantic_result(attempt_payload["request"]["operation"], result)
        )
    if action == "execute_research_read":
        return deep_thaw(validate_research_read_result(result))
    return deep_thaw(result)


def _checkpoint_source_capture_warning(
    observer: CaptureWorkspaceObserver,
) -> dict[str, Any] | None:
    matching = tuple(
        event
        for event in observer.events
        if event.kind is EventKind.BACKUP_CHANGED
        and event.attributes.get("status") in _CHECKPOINT_SOURCE_WARNING_CODES
    )
    if not matching:
        return None
    if len(matching) != 1:
        return None
    attributes = matching[0].attributes
    warning_code = attributes.get("status")
    reason_code = attributes.get("reason_code")
    database_bytes = attributes.get("byte_count")
    duration_seconds = attributes.get("duration_seconds")
    reason_code_valid = (
        warning_code == "checkpoint_source_capture_failed_writer_reacquired"
        and reason_code == "checkpoint_source_capture_failed"
    ) or (
        warning_code == "checkpoint_source_snapshot_skipped_writer_preserved"
        and reason_code in _CHECKPOINT_SOURCE_PRESERVED_WRITER_REASONS
    ) or (
        warning_code
        == "checkpoint_source_operation_teardown_failed_writer_revalidated"
        and reason_code in _CHECKPOINT_SOURCE_TEARDOWN_REASONS
    )
    nullable_measurement = warning_code == (
        "checkpoint_source_snapshot_skipped_writer_preserved"
    ) or (
        warning_code
        == "checkpoint_source_operation_teardown_failed_writer_revalidated"
        and reason_code in _CHECKPOINT_SOURCE_PRESERVED_WRITER_REASONS
    )
    database_measurement_valid = (
        database_bytes is None and nullable_measurement
    ) or (
        isinstance(database_bytes, int)
        and not isinstance(database_bytes, bool)
        and 0 < database_bytes < 1 << 53
    )
    if (
        not isinstance(warning_code, str)
        or warning_code not in _CHECKPOINT_SOURCE_WARNING_CODES
        or not isinstance(reason_code, str)
        or not reason_code_valid
        or not database_measurement_valid
        or isinstance(duration_seconds, bool)
        or not isinstance(duration_seconds, (int, float))
        or not math.isfinite(float(duration_seconds))
        or duration_seconds < 0
    ):
        return None
    handoff_elapsed_ms = int(round(float(duration_seconds) * 1000))
    if handoff_elapsed_ms < 0 or handoff_elapsed_ms >= 1 << 53:
        return None
    return {
        "code": warning_code,
        "reason_code": reason_code,
        "database_bytes": database_bytes,
        "handoff_elapsed_ms": handoff_elapsed_ms,
    }


def _run_host_bridge(
    arguments: argparse.Namespace,
    options: Mapping[str, str | None],
    *,
    warnings: list[dict[str, Any]] | None = None,
) -> Mapping[str, Any]:
    workspace_root, project_id, mission_id = _require_workspace_options(options)
    raw = _read_request(arguments.request)
    action, payload = _parse_host_bridge_request(
        raw, project_id=project_id, mission_id=mission_id
    )
    semantic_request = payload.get("request")
    capture_observer = (
        CaptureWorkspaceObserver()
        if action == "execute_semantic_operation"
        and isinstance(semantic_request, Mapping)
        and semantic_request.get("operation") == "checkpoint"
        else None
    )
    try:
        result = _dispatch_host_bridge_attempt(
            workspace_root=workspace_root,
            project_id=project_id,
            mission_id=mission_id,
            action=action,
            payload=payload,
            observer=capture_observer,
        )
        if capture_observer is not None:
            warning = _checkpoint_source_capture_warning(capture_observer)
            if warning is not None and warnings is not None:
                warnings.append(warning)
        return result
    except sqlite3.OperationalError as exc:
        error_code = getattr(exc, "sqlite_errorcode", None)
        if not isinstance(error_code, int) or error_code & 0xFF != sqlite3.SQLITE_BUSY:
            raise
        raise MissionInterfaceError(
            "mission_operation_unavailable",
            "Mission Store is currently busy; no automatic replay occurred. "
            "Refresh current owner state and explicitly reissue the operation only "
            "if it is still warranted.",
        ) from exc


def _run_domain_command(
    arguments: argparse.Namespace,
    options: Mapping[str, str | None],
) -> Mapping[str, Any]:
    verb = arguments.verb
    dry_run = bool(getattr(arguments, "dry_run", False))
    if verb in _MUTATING_COMMANDS and not dry_run:
        raise MissionInterfaceError(
            "mission_host_bridge_required",
            "Actual semantic mutation requires host-bridge causal binding.",
        )
    workspace_root, project_id, mission_id = _require_workspace_options(options)
    operation = operation_from_cli_verb(verb)
    request = deep_thaw(validate_semantic_request(_read_request(arguments.request)))
    if request["operation"] != operation:
        raise MissionInterfaceError(
            "mission_cli_operation_mismatch",
            "Semantic request operation differs from its CLI verb.",
        )
    interface = MissionInterface.open(workspace_root, project_id, mission_id)
    item = _CLI_ITEMS_BY_VERB[verb]
    method_name = (
        item["preview_method"]
        if dry_run
        else item["execute_method"]
    )
    method = getattr(interface, str(method_name), None)
    if not callable(method):
        raise MissionInterfaceError(
            "mission_operation_unavailable",
            f"MissionInterface.{method_name} is not integrated for GA-R1.",
            missing_owner_primitives=(str(method_name),),
        )
    result = method(request)
    if not isinstance(result, Mapping):
        raise MissionInterfaceError(
            "mission_operation_result_invalid",
            "Mission semantic operation returned no result object.",
        )
    if dry_run:
        return _validate_preview_result(interface, result)
    return deep_thaw(validate_semantic_result(operation, result))


def main(argv: Sequence[str] | None = None) -> int:
    raw = list(sys.argv[1:] if argv is None else argv)
    if not raw:
        raw = ["usage"]
    verb = raw[0] if raw else "usage"
    output_format = "json" if "--json" in raw else "text"
    response_path: Path | None = None
    result_warnings: list[dict[str, Any]] = []

    def emit(payload: Mapping[str, Any]) -> None:
        _emit(payload, output_format, response_path=response_path)

    try:
        cleaned, options = _extract_global_options(raw)
        output_format = str(options["format"])
        arguments = _parser().parse_args(cleaned)
        verb = arguments.verb
        if verb == "host-bridge":
            response_path = _bridge_response_path(
                arguments.request,
                arguments.response,
            )
        if verb == "usage":
            emit(_usage_contract())
            return 0
        if verb == "capabilities":
            payload = _result(
                status="ok",
                verb=verb,
                summary="Reported current noncanonical Mission capabilities.",
                data=deep_thaw(project_operation_capabilities()),
            )
            emit(payload)
            return 0
        if verb == "owner-genesis":
            data = _run_owner_genesis(arguments, options)
        elif verb == "owner-migrate-model-policy":
            data = _run_owner_model_migration(arguments, options)
        elif verb == "owner-revise-scientific-context":
            data = _run_owner_scientific_context_revision(arguments, options)
        elif verb == "owner-bind-scientific-context":
            data = _run_owner_scientific_context_binding(arguments, options)
        elif verb == "owner-reauthorize-mission":
            data = _run_owner_mission_reauthorization(arguments, options)
        elif verb == "host-bridge":
            data = _run_host_bridge(
                arguments,
                options,
                warnings=result_warnings,
            )
        else:
            data = _run_domain_command(arguments, options)
        payload = _result(
            status="ok",
            verb=verb,
            summary="Mission interface command completed.",
            data=data,
            warnings=result_warnings,
        )
        emit(payload)
        return 0
    except CheckpointCommittedSourceHandoffError as exc:
        checkpoint = deep_thaw(exc.checkpoint)
        payload = _result(
            status="error",
            verb=verb,
            summary=(
                "Research checkpoint committed, but post-commit operational "
                "handling failed."
            ),
            data={
                "canonical_effect": "none",
                "mathematical_effect": "none",
                "checkpoint_completed": True,
                "checkpoint": checkpoint,
                "source_publication_state": exc.source_publication_state,
                "source_handoff_failure_code": exc.reason_code,
                "continuation_safety": exc.continuation_safety,
            },
            errors=[
                {
                    "code": exc.code,
                    "reason_code": exc.reason_code,
                    "message": str(exc),
                    "checkpoint_completed": True,
                    "checkpoint_id": checkpoint["checkpoint_id"],
                    "executive_epoch_id": checkpoint["executive_epoch_id"],
                    "checkpoint_state": checkpoint["state"],
                    "source_publication_state": exc.source_publication_state,
                    "continuation_safety": exc.continuation_safety,
                }
            ],
        )
        emit(payload)
        return 1
    except (MissionInterfaceError, MissionOperationContractError) as exc:
        payload = _result(
            status="error",
            verb=verb,
            summary="Mission interface command is blocked by its owner contract.",
            data={
                "stability": "experimental",
                "activation_state": "owner_genesis_and_noncanonical_runtime",
                "canonical_effect": "none",
                "mathematical_effect": "none",
            },
            errors=[
                {
                    "code": exc.code,
                    "message": str(exc),
                    "location": getattr(exc, "location", None),
                    "missing_owner_primitives": list(
                        getattr(exc, "missing_owner_primitives", ())
                    ),
                    "retry_hint": (
                        "Use the model-facing targeted usage guide for this operation "
                        "or run the CLI usage handshake before retrying."
                    ),
                }
            ] + _mission_fence_error_details(exc),
        )
        emit(payload)
        return 1
    except (CheckpointSourceReacquisitionError, CheckpointSourceError) as exc:
        payload = _result(
            status="error",
            verb=verb,
            summary="Mission checkpoint-source handoff failed.",
            data={"canonical_effect": "none", "mathematical_effect": "none"},
            errors=[{"code": exc.code, "message": str(exc)}],
        )
        emit(payload)
        return 1
    except (TypeError, ValueError, StaleCommandError, CommandConflictError) as exc:
        payload = _result(
            status="error",
            verb=verb,
            summary="Mission interface command failed validation.",
            data={"canonical_effect": "none", "mathematical_effect": "none"},
            errors=[
                {
                    "code": "invalid_invocation",
                    "message": str(exc),
                    "retry_hint": f"Run `{ENTRYPOINT} usage --format json`.",
                }
            ] + _mission_fence_error_details(exc),
        )
        emit(payload)
        return 2
    except (OSError, WorkspaceStoreError) as exc:
        payload = _result(
            status="error",
            verb=verb,
            summary="Mission workspace integrity failed.",
            data={"canonical_effect": "none", "mathematical_effect": "none"},
            errors=[
                {
                    "code": "workspace_error",
                    "message": str(exc),
                }
            ] + _mission_fence_error_details(exc),
        )
        emit(payload)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
