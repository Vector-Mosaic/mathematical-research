"""Native Discovery Branch, Mission-wide Strategy, and opportunity projection.

Branch owns question and leverage. Strategy owns integrated Mission attention.
Neither grants mathematical authority. The opportunity portfolio is a pure,
deletable view with no identity, persistence, scoring, allocation, or dispatch.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from typing import Any, Mapping, Sequence

from .json_support import canonical_json_bytes
from .mission_executive import DirectExecutiveEpochAuthority
from .research_model import deep_freeze


FRONTIER_SCHEMA_VERSION = 1

_SHA256_RE = re.compile(r"[0-9a-f]{64}")
_REF_KEYS = {"kind", "identity", "revision", "payload_sha256"}
_BRANCH_KEYS = {
    "schema_version",
    "kind",
    "project_id",
    "mission_id",
    "branch_id",
    "question",
    "leverage_fingerprint",
    "target_hook",
    "genealogy",
    "scoped_failures",
    "non_exclusions",
    "retained_residue",
    "composition_interfaces",
    "recombination_interfaces",
    "revival_conditions",
    "nonclaims",
    "owner_refs",
}
_STRATEGY_KEYS = {
    "schema_version",
    "kind",
    "project_id",
    "mission_id",
    "strategy_id",
    "mission_continuation",
    "integrated_comparison",
    "causal_inputs",
    "serious_opportunities",
    "selected_bets",
    "attention_actions",
    "context_treatment",
    "creativity_treatment",
    "reconsideration_conditions",
    "reversal_conditions",
    "revival_conditions",
    "owner_refs",
}
_BRANCH_STORE_KEYS = {
    "branch_id",
    "mission_id",
    "revision",
    "payload",
    "payload_digest",
    "predecessor_revision",
    "created_actor",
    "created_at",
}
_STRATEGY_STORE_KEYS = {
    "strategy_id",
    "mission_id",
    "revision",
    "payload",
    "payload_digest",
    "predecessor_revision",
    "created_actor",
    "created_at",
}
_GENEALOGY = {"split_from", "merge_of", "successor_of", "supersedes"}
_ATTENTION = {"active", "available", "dormant", "superseded"}
_OPPORTUNITY_RELATION = {"alternative", "complement"}
# Historical Strategy revisions may contain ``pause`` or an unsolved
# ``closeout`` and must remain readable.  Ordinary Executive Strategy authoring
# records only productive continuation.  The exact admitted-result reaction is
# prepared through the private closeout path and is enforced again by Store.
_MISSION_CONTINUATION = {"continue", "pause", "closeout"}
_WRITABLE_MISSION_CONTINUATION = {"continue"}
_FORMAL_REQUEST_PURPOSE = {"targeted_falsification", "targeted_verification"}
_FORMAL_REQUEST_KEYS = {"purpose", "context_ref"}
_SELECTED_BET_KEYS = {"bet", "discriminator", "owner_refs"}
_DEPENDENCY_PREFIX = {
    "mission": "mission",
    "branch": "branch",
    "strategy": "strategy",
    "context": "context",
    "candidate": "candidate",
    "evidence": "evidence",
}
_HOOK_FIELDS = {
    "hook",
    "discriminator",
    "decision_relevance",
    "obstruction",
    "expected_consequence",
    "reversal_condition",
    "owner_refs",
}


def _sha256(value: Mapping[str, Any]) -> str:
    return hashlib.sha256(canonical_json_bytes(value)).hexdigest()


def _text(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{label} must be a non-empty string")
    return value


def _enum(value: Any, label: str, allowed: set[str]) -> str:
    value = _text(value, label)
    if value not in allowed:
        raise ValueError(f"{label} is unsupported")
    return value


def _positive_int(value: Any, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ValueError(f"{label} must be a positive integer")
    return value


def _digest(value: Any, label: str) -> str:
    value = _text(value, label)
    if _SHA256_RE.fullmatch(value) is None:
        raise ValueError(f"{label} must be a lowercase SHA-256")
    return value


def _sequence(value: Any, label: str) -> tuple[Any, ...]:
    if isinstance(value, (str, bytes, bytearray)) or not isinstance(value, Sequence):
        raise ValueError(f"{label} must be an array")
    return tuple(value)


def _mapping(value: Any, label: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ValueError(f"{label} must be an object")
    return value


def _ref(
    value: Any, label: str, *, required_kind: str | None = None
) -> Mapping[str, Any]:
    value = _mapping(value, label)
    if set(value) != _REF_KEYS:
        raise ValueError(f"{label} has the wrong closed reference shape")
    kind = _text(value["kind"], f"{label}.kind")
    if required_kind is not None and kind != required_kind:
        raise ValueError(f"{label} must reference {required_kind}")
    return deep_freeze(
        {
            "kind": kind,
            "identity": _text(value["identity"], f"{label}.identity"),
            "revision": _positive_int(value["revision"], f"{label}.revision"),
            "payload_sha256": _digest(
                value["payload_sha256"], f"{label}.payload_sha256"
            ),
        }
    )


def _ref_key(value: Mapping[str, Any]) -> tuple[str, str, int, str]:
    return (
        str(value["kind"]),
        str(value["identity"]),
        int(value["revision"]),
        str(value["payload_sha256"]),
    )


def _refs(
    values: Any, label: str, *, required_kind: str | None = None
) -> tuple[Mapping[str, Any], ...]:
    result = tuple(
        _ref(item, f"{label}[{index}]", required_kind=required_kind)
        for index, item in enumerate(_sequence(values, label))
    )
    keys = tuple(_ref_key(item) for item in result)
    if len(keys) != len(set(keys)):
        raise ValueError(f"{label} must not repeat an exact owner reference")
    return tuple(sorted(result, key=_ref_key))


def _texts(values: Any, label: str) -> tuple[str, ...]:
    result = tuple(
        _text(item, f"{label}[{index}]")
        for index, item in enumerate(_sequence(values, label))
    )
    if len(result) != len(set(result)):
        raise ValueError(f"{label} must not contain duplicates")
    return tuple(sorted(result))


def _records(
    values: Any,
    label: str,
    *,
    fields: Mapping[str, str],
    enums: Mapping[str, frozenset[str]] | None = None,
    ref_kinds: Mapping[str, str] | None = None,
    nonempty_ref_fields: frozenset[str] = frozenset(),
) -> tuple[Mapping[str, Any], ...]:
    """Normalize a small closed array of semantic records.

    Field kinds are ``text``, ``ref``, or ``refs``. Canonical ordering is only
    deterministic representation; no ordering carries priority.
    """

    enums = enums or {}
    ref_kinds = ref_kinds or {}
    result: list[Mapping[str, Any]] = []
    for index, raw in enumerate(_sequence(values, label)):
        item = _mapping(raw, f"{label}[{index}]")
        if set(item) != set(fields):
            raise ValueError(f"{label}[{index}] has the wrong closed shape")
        normalized: dict[str, Any] = {}
        for key, field_kind in fields.items():
            location = f"{label}[{index}].{key}"
            if field_kind == "text":
                normalized[key] = _text(item[key], location)
                if key in enums and normalized[key] not in enums[key]:
                    raise ValueError(f"{location} is unsupported")
            elif field_kind == "ref":
                normalized[key] = _ref(
                    item[key], location, required_kind=ref_kinds.get(key)
                )
            elif field_kind == "refs":
                normalized[key] = list(_refs(item[key], location))
                if key in nonempty_ref_fields and not normalized[key]:
                    raise ValueError(f"{location} requires exact owner refs")
            else:  # pragma: no cover - module-owned declarative specs
                raise AssertionError(f"unsupported field kind: {field_kind}")
        result.append(deep_freeze(normalized))
    encoded = tuple(canonical_json_bytes(item) for item in result)
    if len(encoded) != len(set(encoded)):
        raise ValueError(f"{label} must not contain duplicate meanings")
    return tuple(item for _, item in sorted(zip(encoded, result), key=lambda pair: pair[0]))


def _selected_bets(values: Any) -> tuple[Mapping[str, Any], ...]:
    """Normalize Strategy bets; an optional formal request binds one Context."""

    result: list[Mapping[str, Any]] = []
    for index, raw in enumerate(_sequence(values, "selected_bets")):
        label = f"selected_bets[{index}]"
        item = _mapping(raw, label)
        if not _SELECTED_BET_KEYS.issubset(item) or set(item) - (
            _SELECTED_BET_KEYS | {"formal_request"}
        ):
            raise ValueError(f"{label} has the wrong closed shape")
        normalized: dict[str, Any] = {
            "bet": _text(item["bet"], f"{label}.bet"),
            "discriminator": _text(
                item["discriminator"], f"{label}.discriminator"
            ),
            "owner_refs": list(_refs(item["owner_refs"], f"{label}.owner_refs")),
        }
        if "formal_request" in item:
            request = _mapping(item["formal_request"], f"{label}.formal_request")
            if set(request) != _FORMAL_REQUEST_KEYS:
                raise ValueError(f"{label}.formal_request has the wrong closed shape")
            normalized["formal_request"] = {
                "purpose": _enum(
                    request["purpose"],
                    f"{label}.formal_request.purpose",
                    _FORMAL_REQUEST_PURPOSE,
                ),
                "context_ref": _ref(
                    request["context_ref"],
                    f"{label}.formal_request.context_ref",
                    required_kind="context",
                ),
            }
        result.append(deep_freeze(normalized))
    encoded = tuple(canonical_json_bytes(item) for item in result)
    if len(encoded) != len(set(encoded)):
        raise ValueError("selected_bets must not contain duplicate meanings")
    return tuple(
        item for _, item in sorted(zip(encoded, result), key=lambda pair: pair[0])
    )


def _normalize_branch(document: Mapping[str, Any]) -> Mapping[str, Any]:
    document = _mapping(document, "Discovery Branch")
    if set(document) != _BRANCH_KEYS:
        raise ValueError("Discovery Branch has the wrong closed shape")
    if (
        document["schema_version"] != FRONTIER_SCHEMA_VERSION
        or document["kind"] != "discovery_branch"
    ):
        raise ValueError("Discovery Branch schema or kind is unsupported")
    branch_id = _text(document["branch_id"], "branch_id")
    genealogy = _records(
        document["genealogy"],
        "genealogy",
        fields={"relationship": "text", "branch": "ref"},
        enums={"relationship": frozenset(_GENEALOGY)},
        ref_kinds={"branch": "branch"},
    )
    if any(item["branch"]["identity"] == branch_id for item in genealogy):
        raise ValueError("Branch genealogy cannot point to its own identity")
    counts = {
        relation: sum(item["relationship"] == relation for item in genealogy)
        for relation in _GENEALOGY
    }
    if counts["split_from"] > 1:
        raise ValueError("a split descendant has exactly one split_from parent")
    if counts["successor_of"] > 1:
        raise ValueError("a linked successor has exactly one predecessor")
    if counts["supersedes"] > 1:
        raise ValueError("a Branch may directly supersede only one identity")
    if counts["merge_of"] == 1:
        raise ValueError("merge_of requires at least two established identities")
    cited_specs = {
        "scoped_failures": ({"scope": "text", "finding": "text", "owner_refs": "refs"}, True),
        "non_exclusions": ({"scope": "text", "statement": "text", "owner_refs": "refs"}, True),
        "retained_residue": ({"residue": "text", "owner_refs": "refs"}, True),
        "composition_interfaces": ({"interface": "text", "owner_refs": "refs"}, False),
        "recombination_interfaces": ({"interface": "text", "owner_refs": "refs"}, False),
        "revival_conditions": ({"condition": "text", "owner_refs": "refs"}, False),
    }
    normalized = {
        "schema_version": FRONTIER_SCHEMA_VERSION,
        "kind": "discovery_branch",
        "project_id": _text(document["project_id"], "project_id"),
        "mission_id": _text(document["mission_id"], "mission_id"),
        "branch_id": branch_id,
        "question": _text(document["question"], "question"),
        "leverage_fingerprint": _text(
            document["leverage_fingerprint"], "leverage_fingerprint"
        ),
        "target_hook": _text(document["target_hook"], "target_hook"),
        "genealogy": list(genealogy),
        "nonclaims": list(_texts(document["nonclaims"], "nonclaims")),
        "owner_refs": list(_refs(document["owner_refs"], "owner_refs")),
    }
    for key, (fields, citations_required) in cited_specs.items():
        normalized[key] = list(
            _records(
                document[key],
                key,
                fields=fields,
                nonempty_ref_fields=(
                    frozenset({"owner_refs"})
                    if citations_required
                    else frozenset()
                ),
            )
        )
    return deep_freeze(normalized)


def _normalize_strategy(document: Mapping[str, Any]) -> Mapping[str, Any]:
    document = _mapping(document, "Mission-wide Strategy")
    if set(document) != _STRATEGY_KEYS:
        raise ValueError("Mission-wide Strategy has the wrong closed shape")
    if (
        document["schema_version"] != FRONTIER_SCHEMA_VERSION
        or document["kind"] != "mission_strategy"
    ):
        raise ValueError("Mission-wide Strategy schema or kind is unsupported")
    attention = _records(
        document["attention_actions"],
        "attention_actions",
        fields={"branch_ref": "ref", "attention": "text", "consequence": "text"},
        enums={"attention": frozenset(_ATTENTION)},
        ref_kinds={"branch_ref": "branch"},
    )
    branch_ids = tuple(str(item["branch_ref"]["identity"]) for item in attention)
    if len(branch_ids) != len(set(branch_ids)):
        raise ValueError("Strategy may assign only one attention action per Branch")
    normalized = {
        "schema_version": FRONTIER_SCHEMA_VERSION,
        "kind": "mission_strategy",
        "project_id": _text(document["project_id"], "project_id"),
        "mission_id": _text(document["mission_id"], "mission_id"),
        "strategy_id": _text(document["strategy_id"], "strategy_id"),
        "mission_continuation": _enum(
            document["mission_continuation"],
            "mission_continuation",
            _MISSION_CONTINUATION,
        ),
        "integrated_comparison": _text(
            document["integrated_comparison"], "integrated_comparison"
        ),
        "causal_inputs": list(
            _records(
                document["causal_inputs"],
                "causal_inputs",
                fields={"source_ref": "ref", "decision_consequence": "text"},
            )
        ),
        "serious_opportunities": list(
            _records(
                document["serious_opportunities"],
                "serious_opportunities",
                fields={
                    "relationship": "text",
                    "opportunity_ref": "ref",
                    "qualitative_opportunity_cost": "text",
                },
                enums={"relationship": frozenset(_OPPORTUNITY_RELATION)},
            )
        ),
        "selected_bets": list(_selected_bets(document["selected_bets"])),
        "attention_actions": list(attention),
        "context_treatment": list(
            _records(
                document["context_treatment"],
                "context_treatment",
                fields={"context_ref": "ref", "treatment": "text"},
                ref_kinds={"context_ref": "context"},
            )
        ),
        "creativity_treatment": list(
            _records(
                document["creativity_treatment"],
                "creativity_treatment",
                fields={
                    "treatment": "text",
                    "decision_consequence": "text",
                    "owner_refs": "refs",
                },
            )
        ),
        "owner_refs": list(_refs(document["owner_refs"], "owner_refs")),
    }
    condition_fields = {"condition": "text", "owner_refs": "refs"}
    for key in (
        "reconsideration_conditions",
        "reversal_conditions",
        "revival_conditions",
    ):
        normalized[key] = list(_records(document[key], key, fields=condition_fields))
    return deep_freeze(normalized)


def _walk_refs(value: Any) -> tuple[Mapping[str, Any], ...]:
    found: list[Mapping[str, Any]] = []

    def visit(item: Any) -> None:
        if isinstance(item, Mapping):
            if set(item) == _REF_KEYS:
                found.append(item)
            else:
                for nested in item.values():
                    visit(nested)
        elif isinstance(item, Sequence) and not isinstance(
            item, (str, bytes, bytearray)
        ):
            for nested in item:
                visit(nested)

    visit(value)
    unique = {_ref_key(item): item for item in found}
    return tuple(unique[key] for key in sorted(unique))


def _dependency_heads(document: Mapping[str, Any]) -> Mapping[str, tuple[int, str]]:
    result: dict[str, tuple[int, str]] = {}
    for reference in _walk_refs(document):
        prefix = _DEPENDENCY_PREFIX.get(str(reference["kind"]))
        if prefix is None:
            continue
        key = f"{prefix}:{reference['identity']}"
        head = (int(reference["revision"]), str(reference["payload_sha256"]))
        if key in result and result[key] != head:
            raise ValueError(f"semantic meaning cites conflicting heads for {key}")
        result[key] = head
    return deep_freeze(dict(sorted(result.items())))


@dataclass(frozen=True, slots=True)
class BranchRevision:
    document: Mapping[str, Any]
    payload_sha256: str

    def __post_init__(self) -> None:
        normalized = _normalize_branch(self.document)
        if canonical_json_bytes(normalized) != canonical_json_bytes(self.document):
            raise ValueError("Discovery Branch document is not canonical")
        object.__setattr__(self, "document", normalized)
        if self.payload_sha256 != _sha256(normalized):
            raise ValueError("Discovery Branch payload digest is stale")


@dataclass(frozen=True, slots=True)
class StrategyRevision:
    document: Mapping[str, Any]
    payload_sha256: str

    def __post_init__(self) -> None:
        normalized = _normalize_strategy(self.document)
        if canonical_json_bytes(normalized) != canonical_json_bytes(self.document):
            raise ValueError("Mission-wide Strategy document is not canonical")
        object.__setattr__(self, "document", normalized)
        if self.payload_sha256 != _sha256(normalized):
            raise ValueError("Mission-wide Strategy payload digest is stale")


def _head_pair(revision: int | None, digest: str | None, label: str) -> None:
    if (revision is None) != (digest is None):
        raise ValueError(f"expected {label} head revision and digest travel together")
    if revision is not None:
        _positive_int(revision, f"expected {label} head revision")
        _digest(digest, f"expected {label} head payload digest")


@dataclass(frozen=True, slots=True)
class PreparedBranchRevision:
    record: BranchRevision
    expected_head_revision: int | None
    expected_head_payload_digest: str | None

    def __post_init__(self) -> None:
        if type(self.record) is not BranchRevision:
            raise TypeError("prepared Branch requires an exact BranchRevision")
        _head_pair(
            self.expected_head_revision, self.expected_head_payload_digest, "Branch"
        )


@dataclass(frozen=True, slots=True)
class PreparedStrategyRevision:
    record: StrategyRevision
    expected_head_revision: int
    expected_head_payload_digest: str

    def __post_init__(self) -> None:
        if type(self.record) is not StrategyRevision:
            raise TypeError("prepared Strategy requires an exact StrategyRevision")
        _positive_int(self.expected_head_revision, "expected Strategy head revision")
        _digest(
            self.expected_head_payload_digest, "expected Strategy head payload digest"
        )


def _lineage(revision: int, predecessor: int | None, label: str) -> None:
    _positive_int(revision, f"{label} revision")
    if predecessor is None:
        if revision != 1:
            raise ValueError(f"only the first {label} revision lacks a predecessor")
    elif _positive_int(predecessor, f"{label} predecessor") != revision - 1:
        raise ValueError(f"{label} predecessor is not contiguous")


@dataclass(frozen=True, slots=True)
class PersistedBranchRevision:
    record: BranchRevision
    revision: int
    predecessor_revision: int | None
    created_actor: str
    created_at: str

    def __post_init__(self) -> None:
        if type(self.record) is not BranchRevision:
            raise TypeError("persisted Branch requires an exact BranchRevision")
        _lineage(self.revision, self.predecessor_revision, "Branch")
        _text(self.created_actor, "Branch created_actor")
        _text(self.created_at, "Branch created_at")

    def to_reference(self) -> Mapping[str, Any]:
        return deep_freeze(
            {
                "kind": "branch",
                "identity": self.record.document["branch_id"],
                "revision": self.revision,
                "payload_sha256": self.record.payload_sha256,
            }
        )


@dataclass(frozen=True, slots=True)
class PersistedStrategyRevision:
    record: StrategyRevision
    revision: int
    predecessor_revision: int | None
    created_actor: str
    created_at: str

    def __post_init__(self) -> None:
        if type(self.record) is not StrategyRevision:
            raise TypeError("persisted Strategy requires an exact StrategyRevision")
        _lineage(self.revision, self.predecessor_revision, "Strategy")
        _text(self.created_actor, "Strategy created_actor")
        _text(self.created_at, "Strategy created_at")

    def to_reference(self) -> Mapping[str, Any]:
        return deep_freeze(
            {
                "kind": "strategy",
                "identity": self.record.document["strategy_id"],
                "revision": self.revision,
                "payload_sha256": self.record.payload_sha256,
            }
        )


def _branch_record(document: Mapping[str, Any]) -> BranchRevision:
    normalized = _normalize_branch(document)
    return BranchRevision(normalized, _sha256(normalized))


def _strategy_record(document: Mapping[str, Any]) -> StrategyRevision:
    normalized = _normalize_strategy(document)
    return StrategyRevision(normalized, _sha256(normalized))


def prepare_branch_revision(
    store: Any,
    *,
    project_id: str,
    mission_id: str,
    branch_id: str,
    question: str,
    leverage_fingerprint: str,
    target_hook: str,
    genealogy: Sequence[Mapping[str, Any]] = (),
    scoped_failures: Sequence[Mapping[str, Any]] = (),
    non_exclusions: Sequence[Mapping[str, Any]] = (),
    retained_residue: Sequence[Mapping[str, Any]] = (),
    composition_interfaces: Sequence[Mapping[str, Any]] = (),
    recombination_interfaces: Sequence[Mapping[str, Any]] = (),
    revival_conditions: Sequence[Mapping[str, Any]] = (),
    nonclaims: Sequence[str] = (),
    owner_refs: Sequence[Mapping[str, Any]] = (),
) -> PreparedBranchRevision:
    """Prepare one target-local Branch revision without assigning attention."""

    document = _normalize_branch(
        {
            "schema_version": FRONTIER_SCHEMA_VERSION,
            "kind": "discovery_branch",
            "project_id": project_id,
            "mission_id": mission_id,
            "branch_id": branch_id,
            "question": question,
            "leverage_fingerprint": leverage_fingerprint,
            "target_hook": target_hook,
            "genealogy": genealogy,
            "scoped_failures": scoped_failures,
            "non_exclusions": non_exclusions,
            "retained_residue": retained_residue,
            "composition_interfaces": composition_interfaces,
            "recombination_interfaces": recombination_interfaces,
            "revival_conditions": revival_conditions,
            "nonclaims": nonclaims,
            "owner_refs": owner_refs,
        }
    )
    record = _branch_record(document)
    current = tuple(
        item
        for item in list_mission_branch_heads(store, mission_id=record.document["mission_id"])
        if item.record.document["branch_id"] == record.document["branch_id"]
    )
    if len(current) > 1:
        raise ValueError("Mission has multiple heads for one Branch identity")
    if not current:
        return PreparedBranchRevision(record, None, None)
    head = current[0]
    if head.record.document["project_id"] != record.document["project_id"]:
        raise ValueError("Branch identity belongs to another project")
    for key in ("question", "leverage_fingerprint", "target_hook"):
        if head.record.document[key] != record.document[key]:
            raise ValueError(
                f"Branch {key} changed; create an explicitly linked successor identity"
            )
    return PreparedBranchRevision(record, head.revision, head.record.payload_sha256)


def commit_branch_revision(
    store: Any,
    *,
    authority: DirectExecutiveEpochAuthority,
    prepared: PreparedBranchRevision,
    lease: Any,
    actor: str,
    expected_dependency_heads: Mapping[str, tuple[int, str]] | None = None,
    command_id: str | None = None,
) -> Any:
    """Commit one Branch meaning through its sole Store facade."""

    if type(prepared) is not PreparedBranchRevision:
        raise TypeError("commit_branch_revision requires PreparedBranchRevision")
    if type(authority) is not DirectExecutiveEpochAuthority:
        raise TypeError("Branch commit requires direct Executive Epoch authority")
    authority.verify_integrity()
    document = prepared.record.document
    if (
        document["project_id"] != authority.project_id
        or document["mission_id"] != authority.mission_id
    ):
        raise ValueError("Branch revision belongs to another current Mission")
    return store.commit_branch_revision(
        executive_epoch_id=authority.executive_epoch_id,
        mission_id=str(document["mission_id"]),
        branch_id=str(document["branch_id"]),
        payload=document,
        expected_head_revision=prepared.expected_head_revision,
        expected_head_payload_digest=prepared.expected_head_payload_digest,
        dependency_heads=(
            _dependency_heads(document)
            if expected_dependency_heads is None
            else expected_dependency_heads
        ),
        lease=lease,
        command_id=command_id
        or f"branch:{document['branch_id']}:{prepared.record.payload_sha256}",
        actor=_text(actor, "Branch actor"),
        expected_canonical_authority_digest=authority.canonical_authority_digest,
    )


def _stored_branch(stored: Mapping[str, Any]) -> PersistedBranchRevision:
    if not isinstance(stored, Mapping) or set(stored) != _BRANCH_STORE_KEYS:
        raise ValueError("stored Branch revision has the wrong closed shape")
    record = _branch_record(stored["payload"])
    if (
        stored["branch_id"] != record.document["branch_id"]
        or stored["mission_id"] != record.document["mission_id"]
        or stored["payload_digest"] != record.payload_sha256
    ):
        raise ValueError("stored Branch metadata disagrees with its payload")
    return PersistedBranchRevision(
        record,
        int(stored["revision"]),
        None
        if stored["predecessor_revision"] is None
        else int(stored["predecessor_revision"]),
        str(stored["created_actor"]),
        str(stored["created_at"]),
    )


def read_branch_revision(
    store: Any,
    *,
    mission_id: str,
    branch_id: str,
    revision: int | None = None,
) -> PersistedBranchRevision:
    mission_id = _text(mission_id, "mission_id")
    branch_id = _text(branch_id, "branch_id")
    if revision is not None:
        _positive_int(revision, "Branch revision")
    result = _stored_branch(
        store.read_branch_revision(
            mission_id=mission_id, branch_id=branch_id, revision=revision
        )
    )
    if (
        result.record.document["mission_id"] != mission_id
        or result.record.document["branch_id"] != branch_id
    ):
        raise ValueError("Store returned a different Branch identity")
    return result


def list_mission_branch_heads(
    store: Any, *, mission_id: str
) -> tuple[PersistedBranchRevision, ...]:
    mission_id = _text(mission_id, "mission_id")
    result = tuple(
        _stored_branch(item) for item in store.list_mission_branch_heads(mission_id)
    )
    identities = tuple(str(item.record.document["branch_id"]) for item in result)
    if any(item.record.document["mission_id"] != mission_id for item in result):
        raise ValueError("Store enumerated a Branch from another Mission")
    if len(identities) != len(set(identities)):
        raise ValueError("Store enumerated multiple heads for one Branch")
    return tuple(sorted(result, key=lambda item: item.record.document["branch_id"]))


def prepare_strategy_revision(
    store: Any,
    *,
    project_id: str,
    mission_id: str,
    strategy_id: str,
    mission_continuation: str,
    integrated_comparison: str,
    causal_inputs: Sequence[Mapping[str, Any]] = (),
    serious_opportunities: Sequence[Mapping[str, Any]] = (),
    selected_bets: Sequence[Mapping[str, Any]] = (),
    attention_actions: Sequence[Mapping[str, Any]] = (),
    context_treatment: Sequence[Mapping[str, Any]] = (),
    creativity_treatment: Sequence[Mapping[str, Any]] = (),
    reconsideration_conditions: Sequence[Mapping[str, Any]] = (),
    reversal_conditions: Sequence[Mapping[str, Any]] = (),
    revival_conditions: Sequence[Mapping[str, Any]] = (),
    owner_refs: Sequence[Mapping[str, Any]] = (),
) -> PreparedStrategyRevision:
    """Prepare an ordinary continuation of the Mission-wide Strategy."""

    return _prepare_strategy_revision(
        store,
        project_id=project_id,
        mission_id=mission_id,
        strategy_id=strategy_id,
        mission_continuation=mission_continuation,
        integrated_comparison=integrated_comparison,
        causal_inputs=causal_inputs,
        serious_opportunities=serious_opportunities,
        selected_bets=selected_bets,
        attention_actions=attention_actions,
        context_treatment=context_treatment,
        creativity_treatment=creativity_treatment,
        reconsideration_conditions=reconsideration_conditions,
        reversal_conditions=reversal_conditions,
        revival_conditions=revival_conditions,
        owner_refs=owner_refs,
        required_continuation="continue",
    )


def _prepare_admitted_result_closeout_strategy_revision(
    store: Any,
    **strategy_fields: Any,
) -> PreparedStrategyRevision:
    """Prepare the internal Strategy reaction to an exact admitted result.

    This helper does not confer authority.  Store re-derives the current exact
    admitted-result projection inside the same transaction as the write.
    """

    return _prepare_strategy_revision(
        store,
        **strategy_fields,
        required_continuation="closeout",
    )


def _prepare_strategy_revision(
    store: Any,
    *,
    project_id: str,
    mission_id: str,
    strategy_id: str,
    mission_continuation: str,
    integrated_comparison: str,
    causal_inputs: Sequence[Mapping[str, Any]] = (),
    serious_opportunities: Sequence[Mapping[str, Any]] = (),
    selected_bets: Sequence[Mapping[str, Any]] = (),
    attention_actions: Sequence[Mapping[str, Any]] = (),
    context_treatment: Sequence[Mapping[str, Any]] = (),
    creativity_treatment: Sequence[Mapping[str, Any]] = (),
    reconsideration_conditions: Sequence[Mapping[str, Any]] = (),
    reversal_conditions: Sequence[Mapping[str, Any]] = (),
    revival_conditions: Sequence[Mapping[str, Any]] = (),
    owner_refs: Sequence[Mapping[str, Any]] = (),
    required_continuation: str,
) -> PreparedStrategyRevision:
    """Prepare one Strategy revision for its exact internal authority lane."""

    current = read_mission_strategy_head(store, mission_id=mission_id)
    if current.record.document["strategy_id"] != strategy_id:
        raise ValueError("record_strategy cannot mint or select a different Strategy id")
    if current.record.document["project_id"] != project_id:
        raise ValueError("Mission-wide Strategy belongs to another project")
    if required_continuation not in {"continue", "closeout"}:
        raise ValueError("Strategy preparation authority is unsupported")
    writable_continuations = (
        _WRITABLE_MISSION_CONTINUATION
        if required_continuation == "continue"
        else {"closeout"}
    )
    if (
        not isinstance(mission_continuation, str)
        or mission_continuation not in writable_continuations
    ):
        if required_continuation == "continue":
            raise ValueError(
                "ordinary Mission-wide Strategy revisions support only continue; "
                "pause and unsolved closeout remain historical readback"
            )
        raise ValueError(
            "the admitted-result Strategy path supports only exact closeout"
        )
    record = _strategy_record(
        {
            "schema_version": FRONTIER_SCHEMA_VERSION,
            "kind": "mission_strategy",
            "project_id": project_id,
            "mission_id": mission_id,
            "strategy_id": strategy_id,
            "mission_continuation": mission_continuation,
            "integrated_comparison": integrated_comparison,
            "causal_inputs": causal_inputs,
            "serious_opportunities": serious_opportunities,
            "selected_bets": selected_bets,
            "attention_actions": attention_actions,
            "context_treatment": context_treatment,
            "creativity_treatment": creativity_treatment,
            "reconsideration_conditions": reconsideration_conditions,
            "reversal_conditions": reversal_conditions,
            "revival_conditions": revival_conditions,
            "owner_refs": owner_refs,
        }
    )
    return PreparedStrategyRevision(
        record, current.revision, current.record.payload_sha256
    )


def commit_strategy_revision(
    store: Any,
    *,
    authority: DirectExecutiveEpochAuthority,
    prepared: PreparedStrategyRevision,
    lease: Any,
    actor: str,
    expected_dependency_heads: Mapping[str, tuple[int, str]] | None = None,
    command_id: str | None = None,
) -> Any:
    """Commit one authored revision of the sole Mission-wide Strategy."""

    if type(prepared) is not PreparedStrategyRevision:
        raise TypeError("commit_strategy_revision requires PreparedStrategyRevision")
    if type(authority) is not DirectExecutiveEpochAuthority:
        raise TypeError("Strategy commit requires direct Executive Epoch authority")
    authority.verify_integrity()
    document = prepared.record.document
    if (
        document["project_id"] != authority.project_id
        or document["mission_id"] != authority.mission_id
    ):
        raise ValueError("Strategy revision belongs to another current Mission")
    return store.commit_strategy_revision(
        executive_epoch_id=authority.executive_epoch_id,
        mission_id=str(document["mission_id"]),
        strategy_id=str(document["strategy_id"]),
        payload=document,
        expected_head_revision=prepared.expected_head_revision,
        expected_head_payload_digest=prepared.expected_head_payload_digest,
        dependency_heads=(
            _dependency_heads(document)
            if expected_dependency_heads is None
            else expected_dependency_heads
        ),
        lease=lease,
        command_id=command_id
        or f"strategy:{document['strategy_id']}:{prepared.record.payload_sha256}",
        actor=_text(actor, "Strategy actor"),
        expected_canonical_authority_digest=authority.canonical_authority_digest,
    )


def _stored_strategy(stored: Mapping[str, Any]) -> PersistedStrategyRevision:
    if not isinstance(stored, Mapping) or set(stored) != _STRATEGY_STORE_KEYS:
        raise ValueError("stored Strategy revision has the wrong closed shape")
    record = _strategy_record(stored["payload"])
    if (
        stored["strategy_id"] != record.document["strategy_id"]
        or stored["mission_id"] != record.document["mission_id"]
        or stored["payload_digest"] != record.payload_sha256
    ):
        raise ValueError("stored Strategy metadata disagrees with its payload")
    return PersistedStrategyRevision(
        record,
        int(stored["revision"]),
        None
        if stored["predecessor_revision"] is None
        else int(stored["predecessor_revision"]),
        str(stored["created_actor"]),
        str(stored["created_at"]),
    )


def read_strategy_revision(
    store: Any,
    *,
    mission_id: str,
    strategy_id: str,
    revision: int | None = None,
) -> PersistedStrategyRevision:
    mission_id = _text(mission_id, "mission_id")
    strategy_id = _text(strategy_id, "strategy_id")
    if revision is not None:
        _positive_int(revision, "Strategy revision")
    result = _stored_strategy(
        store.read_strategy_revision(
            mission_id=mission_id,
            strategy_id=strategy_id,
            revision=revision,
        )
    )
    if (
        result.record.document["mission_id"] != mission_id
        or result.record.document["strategy_id"] != strategy_id
    ):
        raise ValueError("Store returned a different Strategy identity")
    return result


def read_mission_strategy_head(
    store: Any, *, mission_id: str
) -> PersistedStrategyRevision:
    mission_id = _text(mission_id, "mission_id")
    result = _stored_strategy(store.read_mission_strategy_head(mission_id))
    if result.record.document["mission_id"] != mission_id:
        raise ValueError("Store returned a Strategy from another Mission")
    return result


def _hooks(values: Any, label: str) -> tuple[Mapping[str, Any], ...]:
    result: list[Mapping[str, Any]] = []
    for index, raw in enumerate(_sequence(values, label)):
        item = _mapping(raw, f"{label}[{index}]")
        if not {"hook", "owner_refs"}.issubset(item) or not set(item).issubset(
            _HOOK_FIELDS
        ):
            raise ValueError(f"{label}[{index}] has the wrong closed shape")
        normalized = {
            "hook": _text(item["hook"], f"{label}[{index}].hook"),
            "owner_refs": list(_refs(item["owner_refs"], f"{label}[{index}].owner_refs")),
        }
        for key in sorted(set(item) - {"hook", "owner_refs"}):
            normalized[key] = _text(item[key], f"{label}[{index}].{key}")
        result.append(deep_freeze(normalized))
    encoded = tuple(canonical_json_bytes(item) for item in result)
    if len(encoded) != len(set(encoded)):
        raise ValueError(f"{label} must not contain duplicate hooks")
    return tuple(item for _, item in sorted(zip(encoded, result), key=lambda pair: pair[0]))


def derive_opportunity_portfolio(
    *,
    strategy: PersistedStrategyRevision,
    branch_heads: Sequence[PersistedBranchRevision],
    synthesis_hooks: Sequence[Mapping[str, Any]] = (),
    historical_hooks: Sequence[Mapping[str, Any]] = (),
    unincorporated_owner_refs: Sequence[Mapping[str, Any]] = (),
) -> Mapping[str, Any]:
    """Derive the identity-free, read-only opportunity view.

    Canonical array ordering has no priority meaning. Only authored Strategy,
    explicit Branch revival conditions, supplied hooks, and supplied current
    owner deltas appear. No pairwise inference or automatic action occurs.
    """

    if type(strategy) is not PersistedStrategyRevision:
        raise TypeError("opportunity projection requires persisted current Strategy")
    branches = tuple(_sequence(branch_heads, "branch_heads"))
    if any(type(item) is not PersistedBranchRevision for item in branches):
        raise TypeError("opportunity projection requires persisted Branch heads")
    mission_id = strategy.record.document["mission_id"]
    if any(item.record.document["mission_id"] != mission_id for item in branches):
        raise ValueError("opportunity projection cannot cross Mission boundaries")
    by_id = {str(item.record.document["branch_id"]): item for item in branches}
    if len(by_id) != len(branches):
        raise ValueError("opportunity projection received duplicate Branch heads")
    for action in strategy.record.document["attention_actions"]:
        reference = action["branch_ref"]
        current = by_id.get(str(reference["identity"]))
        if current is None or current.to_reference() != reference:
            raise ValueError("Strategy attention cites a stale or absent Branch head")
    revival = tuple(
        deep_freeze(
            {
                "branch_ref": branch.to_reference(),
                "condition": condition["condition"],
                "owner_refs": condition["owner_refs"],
            }
        )
        for branch in branches
        for condition in branch.record.document["revival_conditions"]
    )
    revival = tuple(sorted(revival, key=canonical_json_bytes))
    document = strategy.record.document
    return deep_freeze(
        {
            "strategy_ref": strategy.to_reference(),
            "mission_continuation": document["mission_continuation"],
            "integrated_comparison": document["integrated_comparison"],
            "causal_inputs": document["causal_inputs"],
            "serious_opportunities": document["serious_opportunities"],
            "selected_bets": document["selected_bets"],
            "attention_actions": document["attention_actions"],
            "context_treatment": document["context_treatment"],
            "creativity_treatment": document["creativity_treatment"],
            "reconsideration_conditions": document["reconsideration_conditions"],
            "reversal_conditions": document["reversal_conditions"],
            "revival_conditions": document["revival_conditions"],
            "branch_revival_hooks": revival,
            "synthesis_hooks": _hooks(synthesis_hooks, "synthesis_hooks"),
            "historical_hooks": _hooks(historical_hooks, "historical_hooks"),
            "unincorporated_owner_refs": _refs(
                unincorporated_owner_refs, "unincorporated_owner_refs"
            ),
        }
    )
