"""Direct successor Mission purpose, execution policy, and lifecycle owner."""

from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Any, Mapping, Sequence

from .json_support import canonical_json_bytes
from .research_model import deep_freeze, deep_thaw
from .workspace_schema import IdentityKind, TypedWorkspaceId


SUCCESSOR_MISSION_OBJECTIVE = (
    "Advance the Riemann Hypothesis frontier through the highest-value "
    "mathematically credible work available: select the next question, "
    "delegate adaptively when independent work helps, preserve and interpret "
    "useful results, revise Strategy from what was learned, and commit a "
    "concise truthful Continuation Checkpoint."
)

_DIRECT_MISSION_KEYS = {
    "mission_id",
    "revision",
    "project_id",
    "coordination_epoch",
    "authorization_id",
    "control_revision",
    "lifecycle",
    "fence_reason",
    "fenced_at",
    "autonomous",
    "effective",
    "strategy_ids",
    "scientific_context_id",
    "purpose",
    "execution_policy",
}
_HISTORICAL_DIRECT_MISSION_KEYS = _DIRECT_MISSION_KEYS - {"scientific_context_id"}
_RETAINED_MISSION_HISTORY_KEYS = {"unresolved_decision_bundle_ceiling"}
_CONTRACT_KEYS = {"purpose", "execution_policy"}
_PURPOSE_KEYS = {
    "objective",
    "proof_standard",
    "non_goals",
    "closeout_conditions",
}
_CLOSEOUT_KEYS = {"strategy_mission_continuation", "semantic_effect"}
_EXECUTION_POLICY_KEYS = {
    "provider",
    "model",
    "reasoning_effort",
    "model_fallback",
    "baseline_capabilities",
    "selected_capabilities",
    "network",
    "filesystem",
    "automatic_installation",
}
_NETWORK_KEYS = {
    "public_egress",
    "secretless",
    "credential_inheritance",
    "denied_destinations",
}
_FILESYSTEM_KEYS = {
    "release_access",
    "goal_workspace",
    "protected_credentials_access",
}
_SELECTED_CAPABILITY_KEYS = {"local_roots", "mcp_servers", "apps", "browser"}
_LOCAL_ROOT_KEYS = {"kind", "id", "release_relative_path"}
_MCP_SERVER_KEYS = {"server_id", "enabled_tools"}
_APP_KEYS = {"app_id", "enabled_tools"}
_LOCAL_ROOT_KINDS = frozenset({"skill", "plugin"})
_BROWSER_SELECTION = "isolated_ephemeral_unauthenticated"
_CURRENT_MODEL = "gpt-6-astra"
_HISTORICAL_MODEL = "gpt-5.6-sol"


class MissionOwnerError(ValueError):
    """One exact direct Mission contract or lifecycle error."""

    def __init__(self, code: str, location: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.location = location
        self.message = message


class MissionFenceReason(str, Enum):
    """Closed reasons retained on an ineffective direct Mission revision."""

    EXPIRED = "expired"
    REVOKED = "revoked"
    SUPERSEDED = "superseded"


def _error(code: str, location: str, message: str) -> MissionOwnerError:
    return MissionOwnerError(code, location, message)


def _mapping(value: Any, location: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise _error("mission_contract_invalid", location, "must be an object")
    return value


def _sequence(value: Any, location: str) -> Sequence[Any]:
    if isinstance(value, (str, bytes, bytearray)) or not isinstance(value, Sequence):
        raise _error("mission_contract_invalid", location, "must be an array")
    return value


def _exact(value: Mapping[str, Any], keys: set[str], location: str) -> None:
    missing = sorted(keys - set(value))
    extra = sorted(set(value) - keys)
    if missing or extra:
        raise _error(
            "mission_contract_invalid",
            location,
            f"closed object mismatch; missing={missing}, extra={extra}",
        )


def _text(value: Any, location: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise _error("mission_contract_invalid", location, "must be a non-empty string")
    return value


def _positive_int(value: Any, location: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise _error("mission_contract_invalid", location, "must be a positive integer")
    return value


def _iso(value: Any, location: str) -> datetime:
    text = _text(value, location)
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise _error(
            "mission_contract_invalid",
            location,
            "must be an ISO-8601 timestamp with a timezone",
        ) from exc
    if parsed.tzinfo is None:
        raise _error(
            "mission_contract_invalid",
            location,
            "must be an ISO-8601 timestamp with a timezone",
        )
    return parsed


def _capability_identifier(value: Any, location: str) -> str:
    text = _text(value, location)
    if text != text.strip() or any(
        ord(character) < 32 or ord(character) == 127 for character in text
    ):
        raise _error(
            "successor_mission_contract_invalid",
            location,
            "must be one exact trimmed identifier without control characters",
        )
    return text


def successor_mission_contract(
    contract: Mapping[str, Any] | None = None,
    *,
    selected_capabilities: Mapping[str, Any] | None = None,
) -> Mapping[str, Any]:
    """Build or validate the one direct successor Mission-owned contract."""

    if contract is None:
        model = _CURRENT_MODEL
        selections_input: Any = (
            selected_capabilities
            if selected_capabilities is not None
            else {
                "local_roots": [],
                "mcp_servers": [],
                "apps": [],
                "browser": None,
            }
        )
        persisted = None
    else:
        if selected_capabilities is not None:
            raise _error(
                "successor_mission_contract_invalid",
                "selected_capabilities",
                "cannot override selections while validating a persisted contract",
            )
        persisted = _mapping(contract, "successor_mission_contract")
        _exact(persisted, _CONTRACT_KEYS, "successor_mission_contract")
        purpose = _mapping(persisted["purpose"], "successor_mission_contract.purpose")
        _exact(purpose, _PURPOSE_KEYS, "successor_mission_contract.purpose")
        closeout = _mapping(
            purpose["closeout_conditions"],
            "successor_mission_contract.purpose.closeout_conditions",
        )
        _exact(
            closeout,
            _CLOSEOUT_KEYS,
            "successor_mission_contract.purpose.closeout_conditions",
        )
        policy = _mapping(
            persisted["execution_policy"],
            "successor_mission_contract.execution_policy",
        )
        _exact(
            policy,
            _EXECUTION_POLICY_KEYS,
            "successor_mission_contract.execution_policy",
        )
        model = policy["model"]
        if model not in (_HISTORICAL_MODEL, _CURRENT_MODEL):
            raise _error(
                "successor_mission_contract_invalid",
                "successor_mission_contract.execution_policy.model",
                "model must be an exact supported historical or current selection",
            )
        network = _mapping(
            policy["network"],
            "successor_mission_contract.execution_policy.network",
        )
        _exact(
            network,
            _NETWORK_KEYS,
            "successor_mission_contract.execution_policy.network",
        )
        filesystem = _mapping(
            policy["filesystem"],
            "successor_mission_contract.execution_policy.filesystem",
        )
        _exact(
            filesystem,
            _FILESYSTEM_KEYS,
            "successor_mission_contract.execution_policy.filesystem",
        )
        selections_input = policy["selected_capabilities"]

    selections = _mapping(
        selections_input,
        "successor_mission_contract.execution_policy.selected_capabilities",
    )
    _exact(
        selections,
        _SELECTED_CAPABILITY_KEYS,
        "successor_mission_contract.execution_policy.selected_capabilities",
    )

    normalized_roots: list[Mapping[str, str]] = []
    root_ids: set[str] = set()
    root_paths: set[str] = set()
    for index, raw_root in enumerate(
        _sequence(
            selections["local_roots"],
            "successor_mission_contract.execution_policy."
            "selected_capabilities.local_roots",
        )
    ):
        location = (
            "successor_mission_contract.execution_policy."
            f"selected_capabilities.local_roots[{index}]"
        )
        root = _mapping(raw_root, location)
        _exact(root, _LOCAL_ROOT_KEYS, location)
        kind = _text(root["kind"], f"{location}.kind")
        if kind not in _LOCAL_ROOT_KINDS:
            raise _error(
                "successor_mission_contract_invalid",
                f"{location}.kind",
                "must be skill or plugin",
            )
        root_id = _capability_identifier(root["id"], f"{location}.id")
        relative_path = _text(
            root["release_relative_path"], f"{location}.release_relative_path"
        )
        path_parts = relative_path.split("/")
        if (
            relative_path.startswith("/")
            or "\\" in relative_path
            or any(part in {"", ".", ".."} for part in path_parts)
        ):
            raise _error(
                "successor_mission_contract_invalid",
                f"{location}.release_relative_path",
                "must be one canonical path strictly relative to the immutable release",
            )
        if root_id in root_ids or relative_path in root_paths:
            raise _error(
                "successor_mission_contract_invalid",
                location,
                "selected local root ids and release-relative paths must be unique",
            )
        root_ids.add(root_id)
        root_paths.add(relative_path)
        normalized_roots.append(
            {
                "kind": kind,
                "id": root_id,
                "release_relative_path": relative_path,
            }
        )

    def normalize_tool_selections(
        raw_value: Any,
        *,
        family: str,
        keys: set[str],
        id_key: str,
    ) -> list[Mapping[str, Any]]:
        normalized: list[Mapping[str, Any]] = []
        identities: set[str] = set()
        for index, raw_selection in enumerate(
            _sequence(
                raw_value,
                "successor_mission_contract.execution_policy."
                f"selected_capabilities.{family}",
            )
        ):
            location = (
                "successor_mission_contract.execution_policy."
                f"selected_capabilities.{family}[{index}]"
            )
            selection = _mapping(raw_selection, location)
            _exact(selection, keys, location)
            identity = _capability_identifier(selection[id_key], f"{location}.{id_key}")
            tools = tuple(
                _capability_identifier(item, f"{location}.enabled_tools[{tool_index}]")
                for tool_index, item in enumerate(
                    _sequence(selection["enabled_tools"], f"{location}.enabled_tools")
                )
            )
            if not tools:
                raise _error(
                    "successor_mission_contract_invalid",
                    f"{location}.enabled_tools",
                    "must name at least one exact tool when selected",
                )
            if len(tools) != len(set(tools)):
                raise _error(
                    "successor_mission_contract_invalid",
                    f"{location}.enabled_tools",
                    "contains duplicate tool identifiers",
                )
            if identity in identities:
                raise _error(
                    "successor_mission_contract_invalid",
                    f"{location}.{id_key}",
                    "contains a duplicate capability identifier",
                )
            identities.add(identity)
            normalized.append({id_key: identity, "enabled_tools": list(tools)})
        return normalized

    normalized_mcp_servers = normalize_tool_selections(
        selections["mcp_servers"],
        family="mcp_servers",
        keys=_MCP_SERVER_KEYS,
        id_key="server_id",
    )
    normalized_apps = normalize_tool_selections(
        selections["apps"],
        family="apps",
        keys=_APP_KEYS,
        id_key="app_id",
    )
    browser = selections["browser"]
    if browser is not None and browser != _BROWSER_SELECTION:
        raise _error(
            "successor_mission_contract_invalid",
            "successor_mission_contract.execution_policy.selected_capabilities.browser",
            "must be null or the isolated ephemeral unauthenticated browser",
        )

    expected = {
        "purpose": {
            "objective": SUCCESSOR_MISSION_OBJECTIVE,
            "proof_standard": (
                "A genuinely correct, novel, self-contained, independently "
                "checkable proof or disproof of the Riemann Hypothesis whose "
                "exact statement, dependencies, derivation, assumptions, and "
                "verification package withstand independent reconstruction; "
                "computation, model confidence, repeated agreement, or system "
                "state cannot establish it."
            ),
            "non_goals": [
                (
                    "Claiming a theorem from finite moments, bounded-degree "
                    "tests, floating-point evidence, model agreement, or a "
                    "green verification target."
                ),
                (
                    "Owning unrelated application or workflow orchestration."
                ),
                "Treating the system as an automated theorem prover.",
                (
                    "Requiring the operator to act as the mathematical strategist or "
                    "routine proof reviewer, or treating AI agreement as a "
                    "substitute for checkable mathematics."
                ),
                (
                    "Treating an immortal process, unattended daemon, completed "
                    "Goal, worker count, artifact count, or infrastructure "
                    "qualification as research success."
                ),
            ],
            "closeout_conditions": {
                "strategy_mission_continuation": "closeout",
                "semantic_effect": (
                    "Closeout is a Strategy-owned coordination decision; it "
                    "does not establish proof, disproof, Admission, or research "
                    "success."
                ),
            },
        },
        "execution_policy": {
            "provider": "openai",
            "model": model,
            "reasoning_effort": "ultra",
            "model_fallback": False,
            "baseline_capabilities": [
                "rh_mission",
                "shell",
                "web_search",
                "native_delegation",
            ],
            "selected_capabilities": {
                "local_roots": normalized_roots,
                "mcp_servers": normalized_mcp_servers,
                "apps": normalized_apps,
                "browser": browser,
            },
            "network": {
                "public_egress": True,
                "secretless": True,
                "credential_inheritance": False,
                "denied_destinations": [
                    "loopback",
                    "private_internal",
                    "link_local",
                    "cloud_metadata",
                ],
            },
            "filesystem": {
                "release_access": "read_only",
                "goal_workspace": "fresh_private_writable",
                "protected_credentials_access": "denied",
            },
            "automatic_installation": False,
        },
    }
    if persisted is not None and canonical_json_bytes(
        persisted
    ) != canonical_json_bytes(expected):
        raise _error(
            "successor_mission_contract_invalid",
            "successor_mission_contract",
            "persisted purpose or execution policy differs from the successor Mission contract",
        )
    return deep_freeze(expected)


def _direct_mission_lifecycle_source(mission: Mapping[str, Any]) -> Mapping[str, Any]:
    record = _mapping(mission, "mission")
    # Converted predecessors may retain this obsolete field as immutable
    # history. Lifecycle successors carry it unchanged, never as Goal policy.
    # Fresh genesis has its own closed seed shape and cannot create it.
    _exact(
        record,
        (
            _DIRECT_MISSION_KEYS
            if "scientific_context_id" in record
            else _HISTORICAL_DIRECT_MISSION_KEYS
        ) | (_RETAINED_MISSION_HISTORY_KEYS & set(record)),
        "mission",
    )
    successor_mission_contract(
        {
            "purpose": record["purpose"],
            "execution_policy": record["execution_policy"],
        }
    )
    _text(record["mission_id"], "mission.mission_id")
    _scientific_context_identity(record.get("scientific_context_id"))
    return record


def _scientific_context_identity(value: Any) -> str | None:
    if value is None:
        return None
    try:
        identity = TypedWorkspaceId(IdentityKind.CONTEXT, value)
    except (TypeError, ValueError) as exc:
        raise _error(
            "mission_contract_invalid",
            "mission.scientific_context_id",
            "must be null or one bare Context identity",
        ) from exc
    if identity.value.startswith("context:"):
        raise _error(
            "mission_contract_invalid",
            "mission.scientific_context_id",
            "must be a bare Context identity, not a typed reference",
        )
    return identity.value


def _mission_successor_document(
    record: Mapping[str, Any], *, preserve_historical_shape: bool,
) -> dict[str, Any]:
    """Keep old effects reconstructable; ordinary writes always use new shape.

    The opt-in is solely for exact historical journal-effect authentication,
    never a write-time fallback. Reading historical input does not mutate it.
    """
    successor = deep_thaw(record)
    if not preserve_historical_shape:
        successor.setdefault("scientific_context_id", None)
    return successor


def direct_mission_scientific_context_successor(
    mission: Mapping[str, Any], *, context_id: str,
) -> Mapping[str, Any]:
    """Derive an explicitly selected binding, without changing research meaning.

    Writer custody, an inactive Mission runtime, exact target v3 scope and all
    current-head preimages are checked atomically by the sole Store writer.
    No Context is created and no target is inferred from Strategy or recency.
    """
    record = _direct_mission_lifecycle_source(mission)
    selected = _scientific_context_identity(context_id)
    if selected is None:
        raise _error(
            "mission_scientific_context_binding_invalid",
            "context_id",
            "binding requires one explicitly selected Context identity",
        )
    successor = _mission_successor_document(record, preserve_historical_shape=False)
    successor["scientific_context_id"] = selected
    successor["control_revision"] = _positive_int(
        record["control_revision"], "mission.control_revision"
    ) + 1
    return deep_freeze(successor)


def direct_mission_astra_successor(
    mission: Mapping[str, Any],
    *,
    preserve_historical_shape: bool = False,
) -> Mapping[str, Any]:
    """Derive only the owner-authorized Sol/ultra to Astra/ultra revision."""

    record = _direct_mission_lifecycle_source(mission)
    if (
        record["lifecycle"] != "active"
        or record["effective"] is not True
        or record["fence_reason"] is not None
        or record["fenced_at"] is not None
    ):
        raise _error(
            "mission_lifecycle_invalid",
            "mission",
            "model migration requires one current effective unfenced Mission",
        )
    if record["execution_policy"]["model"] != _HISTORICAL_MODEL:
        raise _error(
            "mission_model_migration_invalid",
            "mission.execution_policy.model",
            "only the exact Sol to Astra model migration is supported",
        )
    control_revision = _positive_int(
        record["control_revision"], "mission.control_revision"
    )
    successor = _mission_successor_document(
        record, preserve_historical_shape=preserve_historical_shape
    )
    successor["execution_policy"]["model"] = _CURRENT_MODEL
    successor["control_revision"] = control_revision + 1
    return deep_freeze(successor)


def direct_mission_reauthorization_successor(
    mission: Mapping[str, Any],
    *,
    preserve_historical_shape: bool = False,
) -> Mapping[str, Any]:
    """Derive a fresh owner grant while retaining the revoked revision as history.

    The original authorization_id remains genesis provenance. The immutable
    reauthorization command and new Mission revision carry the renewed grant;
    the Store separately verifies the exact incident fence and terminal.
    """

    record = _direct_mission_lifecycle_source(mission)
    if (
        record["lifecycle"] != "held"
        or record["effective"] is not False
        or record["fence_reason"] != MissionFenceReason.REVOKED.value
    ):
        raise _error(
            "mission_lifecycle_invalid",
            "mission",
            "reauthorization requires one exact revoked Mission",
        )
    _iso(record["fenced_at"], "mission.fenced_at")
    control_revision = _positive_int(
        record["control_revision"], "mission.control_revision"
    )
    successor = _mission_successor_document(
        record, preserve_historical_shape=preserve_historical_shape
    )
    successor.update(
        {
            "lifecycle": "active",
            "effective": True,
            "fence_reason": None,
            "fenced_at": None,
            "control_revision": control_revision + 1,
        }
    )
    return deep_freeze(successor)


def direct_mission_fence_successor(
    mission: Mapping[str, Any],
    *,
    fenced_at: str,
    preserve_historical_shape: bool = False,
) -> Mapping[str, Any]:
    """Derive the sole revoked successor of one exact direct Mission owner."""

    record = _direct_mission_lifecycle_source(mission)
    if (
        record["lifecycle"] != "active"
        or record["effective"] is not True
        or record["fence_reason"] is not None
        or record["fenced_at"] is not None
    ):
        raise _error(
            "mission_lifecycle_invalid",
            "mission",
            "only one current effective unfenced Mission may be directly fenced",
        )
    control_revision = _positive_int(
        record["control_revision"], "mission.control_revision"
    )
    _iso(fenced_at, "fenced_at")
    successor = _mission_successor_document(
        record, preserve_historical_shape=preserve_historical_shape
    )
    successor.update(
        {
            "lifecycle": "held",
            "effective": False,
            "fence_reason": MissionFenceReason.REVOKED.value,
            "fenced_at": fenced_at,
            "control_revision": control_revision + 1,
        }
    )
    return deep_freeze(successor)


__all__ = [
    "MissionFenceReason",
    "MissionOwnerError",
    "SUCCESSOR_MISSION_OBJECTIVE",
    "direct_mission_astra_successor",
    "direct_mission_fence_successor",
    "direct_mission_reauthorization_successor",
    "direct_mission_scientific_context_successor",
    "successor_mission_contract",
]
