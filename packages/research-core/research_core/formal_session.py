"""Conditional formal Research Session meaning and direct-owner persistence.

A formal Session exists only for a Strategy bet that explicitly requests the
formal lane.  Mathematical Research owns the immutable semantic request and a
single open-to-terminal binding.  Workstation Control continues to own every
concrete Attempt, provider effect, journal, and sealed operational artifact.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from typing import Any, Mapping, Sequence

from .context_revision import PersistedContextRevision, read_context_revision
from .json_support import canonical_json_bytes
from .mission_evidence import read_raw_capture
from .mission_executive import (
    DirectExecutiveEpochAuthority,
    reissue_direct_executive_epoch_authority,
)
from .mission_frontier import (
    PersistedStrategyRevision,
    read_mission_strategy_head,
    read_strategy_revision,
)
from .research_model import deep_freeze, deep_thaw
from .workspace_store import (
    IdentityKind,
    RevisionRef,
    StaleCommandError,
    TypedWorkspaceId,
    WorkspaceIntegrityError,
)


FORMAL_SESSION_SCHEMA_VERSION = 1
_FORMAL_SESSION_IDENTITY_DOMAIN = "mathematical_research.formal_session.identity.v1"
_VERIFIED_RESULT_ISSUER = object()

_SHA256_RE = re.compile(r"[0-9a-f]{64}")
_OWNER_REF_KEYS = {"kind", "identity", "revision", "payload_sha256"}
_SELECTED_BET_KEYS = {"bet", "discriminator", "owner_refs", "formal_request"}
_FORMAL_REQUEST_KEYS = {"purpose", "context_ref"}
_FORMAL_REQUEST_PURPOSES = {
    "targeted_falsification",
    "targeted_verification",
}
_ATTEMPT_RESULT_REF_KEYS = {
    "kind",
    "attempt_id",
    "session_id",
    "attempt_state",
    "result_digest_sha256",
}
_RAW_CAPTURE_REF_KEYS = {
    "kind",
    "capture_id",
    "capture_digest_sha256",
}
_TERMINAL_BINDING_KEYS = {"attempt_result_ref", "raw_capture_ref"}
_SETTLEABLE_ATTEMPT_STATES = {
    "succeeded",
    "failed",
    "cancelled",
    "force_stopped",
    "fenced",
}
_SESSION_KEYS = {
    "schema_version",
    "kind",
    "project_id",
    "mission_id",
    "session_id",
    "lifecycle",
    "mission_ref",
    "strategy_ref",
    "selected_bet_sha256",
    "context_ref",
    "terminal_binding",
}
_SESSION_STORE_KEYS = {
    "session_id",
    "mission_id",
    "revision",
    "payload",
    "payload_digest",
    "predecessor_revision",
    "created_actor",
    "created_at",
}


def _sha256(value: Mapping[str, Any]) -> str:
    return hashlib.sha256(canonical_json_bytes(value)).hexdigest()


def _text(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{label} must be a non-empty string")
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


def _owner_ref(
    value: Any,
    label: str,
    *,
    required_kind: str | None = None,
) -> Mapping[str, Any]:
    value = _mapping(value, label)
    if set(value) != _OWNER_REF_KEYS:
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


def _owner_ref_key(value: Mapping[str, Any]) -> tuple[str, str, int, str]:
    return (
        str(value["kind"]),
        str(value["identity"]),
        int(value["revision"]),
        str(value["payload_sha256"]),
    )


def _session_identity_material(
    *,
    project_id: str,
    mission_ref: Mapping[str, Any],
    strategy_ref: Mapping[str, Any],
    selected_bet_sha256: str,
    context_ref: Mapping[str, Any],
) -> Mapping[str, Any]:
    """Return the closed, domain-separated immutable Session identity input."""

    return deep_freeze(
        {
            "domain": _FORMAL_SESSION_IDENTITY_DOMAIN,
            "project_id": _text(project_id, "project_id"),
            "mission_ref": _owner_ref(
                mission_ref,
                "mission_ref",
                required_kind="mission",
            ),
            "strategy_ref": _owner_ref(
                strategy_ref,
                "strategy_ref",
                required_kind="strategy",
            ),
            "selected_bet_sha256": _digest(
                selected_bet_sha256,
                "selected_bet_sha256",
            ),
            "context_ref": _owner_ref(
                context_ref,
                "context_ref",
                required_kind="context",
            ),
        }
    )


def canonical_formal_session_id(
    *,
    project_id: str,
    mission_ref: Mapping[str, Any],
    strategy_ref: Mapping[str, Any],
    selected_bet_sha256: str,
    context_ref: Mapping[str, Any],
) -> str:
    """Derive the sole Session identity for one exact formal Strategy request."""

    material = _session_identity_material(
        project_id=project_id,
        mission_ref=mission_ref,
        strategy_ref=strategy_ref,
        selected_bet_sha256=selected_bet_sha256,
        context_ref=context_ref,
    )
    return f"session.formal.{_sha256(material)}"


def _owner_refs(value: Any, label: str) -> tuple[Mapping[str, Any], ...]:
    result = tuple(
        _owner_ref(item, f"{label}[{index}]")
        for index, item in enumerate(_sequence(value, label))
    )
    keys = tuple(_owner_ref_key(item) for item in result)
    if len(keys) != len(set(keys)):
        raise ValueError(f"{label} must not repeat an exact owner reference")
    return tuple(sorted(result, key=_owner_ref_key))


def _selected_bet(value: Any) -> Mapping[str, Any]:
    value = _mapping(value, "selected_bet")
    if set(value) != _SELECTED_BET_KEYS:
        raise ValueError("selected_bet must be one exact Strategy formal request")
    request = _mapping(value["formal_request"], "selected_bet.formal_request")
    if set(request) != _FORMAL_REQUEST_KEYS:
        raise ValueError("selected_bet.formal_request has the wrong closed shape")
    purpose = _text(request["purpose"], "selected_bet.formal_request.purpose")
    if purpose not in _FORMAL_REQUEST_PURPOSES:
        raise ValueError("selected_bet.formal_request purpose is unsupported")
    return deep_freeze(
        {
            "bet": _text(value["bet"], "selected_bet.bet"),
            "discriminator": _text(
                value["discriminator"], "selected_bet.discriminator"
            ),
            "owner_refs": list(
                _owner_refs(value["owner_refs"], "selected_bet.owner_refs")
            ),
            "formal_request": {
                "purpose": purpose,
                "context_ref": _owner_ref(
                    request["context_ref"],
                    "selected_bet.formal_request.context_ref",
                    required_kind="context",
                ),
            },
        }
    )


def _attempt_result_ref(value: Any, *, session_id: str) -> Mapping[str, Any]:
    value = _mapping(value, "attempt_result_ref")
    if set(value) != _ATTEMPT_RESULT_REF_KEYS:
        raise ValueError("attempt_result_ref has the wrong closed shape")
    if value["kind"] != "workstation_attempt_result":
        raise ValueError("attempt_result_ref kind is unsupported")
    referenced_session_id = _text(value["session_id"], "attempt_result_ref.session_id")
    if referenced_session_id != session_id:
        raise ValueError("Attempt result belongs to another formal Session")
    attempt_state = _text(
        value["attempt_state"],
        "attempt_result_ref.attempt_state",
    )
    if attempt_state not in _SETTLEABLE_ATTEMPT_STATES:
        raise ValueError("Attempt result is not a settleable terminal fact")
    return deep_freeze(
        {
            "kind": "workstation_attempt_result",
            "attempt_id": _text(value["attempt_id"], "attempt_result_ref.attempt_id"),
            "session_id": referenced_session_id,
            "attempt_state": attempt_state,
            "result_digest_sha256": _digest(
                value["result_digest_sha256"],
                "attempt_result_ref.result_digest_sha256",
            ),
        }
    )


def _raw_capture_ref(value: Any) -> Mapping[str, Any]:
    value = _mapping(value, "raw_capture_ref")
    if set(value) != _RAW_CAPTURE_REF_KEYS:
        raise ValueError("raw_capture_ref has the wrong closed shape")
    if value["kind"] != "raw_capture":
        raise ValueError("raw_capture_ref kind is unsupported")
    return deep_freeze(
        {
            "kind": "raw_capture",
            "capture_id": _text(value["capture_id"], "raw_capture_ref.capture_id"),
            "capture_digest_sha256": _digest(
                value["capture_digest_sha256"],
                "raw_capture_ref.capture_digest_sha256",
            ),
        }
    )


def _terminal_binding(value: Any, *, session_id: str) -> Mapping[str, Any]:
    value = _mapping(value, "terminal_binding")
    if set(value) != _TERMINAL_BINDING_KEYS:
        raise ValueError("terminal_binding has the wrong closed shape")
    return deep_freeze(
        {
            "attempt_result_ref": _attempt_result_ref(
                value["attempt_result_ref"], session_id=session_id
            ),
            "raw_capture_ref": (
                None
                if value["raw_capture_ref"] is None
                else _raw_capture_ref(value["raw_capture_ref"])
            ),
        }
    )


@dataclass(frozen=True, slots=True, init=False)
class VerifiedFormalSessionResult:
    """Bridge-issued proof that one WC result is safe to bind as a terminal fact."""

    session_id: str
    attempt_result_ref: Mapping[str, Any]
    raw_capture_ref: Mapping[str, Any] | None
    _issuer: object = field(repr=False, compare=False)

    def __init__(
        self,
        *,
        session_id: str,
        attempt_result_ref: Mapping[str, Any],
        raw_capture_ref: Mapping[str, Any] | None,
        _issuer: object,
    ) -> None:
        if _issuer is not _VERIFIED_RESULT_ISSUER:
            raise TypeError(
                "VerifiedFormalSessionResult may be issued only by the MR-WC bridge"
            )
        normalized_session_id = _text(session_id, "session_id")
        object.__setattr__(self, "session_id", normalized_session_id)
        object.__setattr__(
            self,
            "attempt_result_ref",
            _attempt_result_ref(
                attempt_result_ref,
                session_id=normalized_session_id,
            ),
        )
        object.__setattr__(
            self,
            "raw_capture_ref",
            None if raw_capture_ref is None else _raw_capture_ref(raw_capture_ref),
        )
        object.__setattr__(self, "_issuer", _issuer)

    def verify_issued(self) -> None:
        if self._issuer is not _VERIFIED_RESULT_ISSUER:
            raise TypeError(
                "VerifiedFormalSessionResult was not issued by the MR-WC bridge"
            )

    @property
    def terminal_binding(self) -> Mapping[str, Any]:
        self.verify_issued()
        return deep_freeze(
            {
                "attempt_result_ref": self.attempt_result_ref,
                "raw_capture_ref": self.raw_capture_ref,
            }
        )


def _issue_verified_formal_session_result(
    *,
    session_id: str,
    attempt_id: str,
    attempt_state: str,
    result_digest_sha256: str,
    raw_capture_id: str | None,
    raw_capture_digest_sha256: str | None,
) -> VerifiedFormalSessionResult:
    """Issue the narrow terminal capability after bridge-owned WC verification."""

    if (raw_capture_id is None) != (raw_capture_digest_sha256 is None):
        raise ValueError("raw capture identity and digest must travel together")
    raw_capture_ref = (
        None
        if raw_capture_id is None
        else {
            "kind": "raw_capture",
            "capture_id": raw_capture_id,
            "capture_digest_sha256": raw_capture_digest_sha256,
        }
    )
    return VerifiedFormalSessionResult(
        session_id=session_id,
        attempt_result_ref={
            "kind": "workstation_attempt_result",
            "attempt_id": attempt_id,
            "session_id": session_id,
            "attempt_state": attempt_state,
            "result_digest_sha256": result_digest_sha256,
        },
        raw_capture_ref=raw_capture_ref,
        _issuer=_VERIFIED_RESULT_ISSUER,
    )


def _normalize_session(document: Mapping[str, Any]) -> Mapping[str, Any]:
    document = _mapping(document, "formal Session")
    if set(document) != _SESSION_KEYS:
        raise ValueError("formal Session has the wrong closed shape")
    if (
        document["schema_version"] != FORMAL_SESSION_SCHEMA_VERSION
        or document["kind"] != "formal_session"
    ):
        raise ValueError("formal Session schema or kind is unsupported")
    project_id = _text(document["project_id"], "project_id")
    mission_id = _text(document["mission_id"], "mission_id")
    session_id = _text(document["session_id"], "session_id")
    lifecycle = _text(document["lifecycle"], "lifecycle")
    if lifecycle not in {"open", "terminal"}:
        raise ValueError("formal Session lifecycle must be open or terminal")
    mission_ref = _owner_ref(
        document["mission_ref"], "mission_ref", required_kind="mission"
    )
    if mission_ref["identity"] != mission_id:
        raise ValueError("formal Session Mission reference has another identity")
    strategy_ref = _owner_ref(
        document["strategy_ref"], "strategy_ref", required_kind="strategy"
    )
    selected_bet_sha256 = _digest(
        document["selected_bet_sha256"], "selected_bet_sha256"
    )
    context_ref = _owner_ref(
        document["context_ref"], "context_ref", required_kind="context"
    )
    expected_session_id = canonical_formal_session_id(
        project_id=project_id,
        mission_ref=mission_ref,
        strategy_ref=strategy_ref,
        selected_bet_sha256=selected_bet_sha256,
        context_ref=context_ref,
    )
    if session_id != expected_session_id:
        raise ValueError(
            "formal Session identity is not its canonical request identity"
        )
    terminal = document["terminal_binding"]
    if lifecycle == "open":
        if terminal is not None:
            raise ValueError("open formal Session cannot carry a terminal binding")
        normalized_terminal = None
    else:
        if terminal is None:
            raise ValueError(
                "terminal formal Session requires its exact result binding"
            )
        normalized_terminal = _terminal_binding(terminal, session_id=session_id)
    return deep_freeze(
        {
            "schema_version": FORMAL_SESSION_SCHEMA_VERSION,
            "kind": "formal_session",
            "project_id": project_id,
            "mission_id": mission_id,
            "session_id": session_id,
            "lifecycle": lifecycle,
            "mission_ref": mission_ref,
            "strategy_ref": strategy_ref,
            "selected_bet_sha256": selected_bet_sha256,
            "context_ref": context_ref,
            "terminal_binding": normalized_terminal,
        }
    )


def _semantic_identity(document: Mapping[str, Any]) -> Mapping[str, Any]:
    normalized = _normalize_session(document)
    return _session_identity_material(
        project_id=str(normalized["project_id"]),
        mission_ref=normalized["mission_ref"],
        strategy_ref=normalized["strategy_ref"],
        selected_bet_sha256=str(normalized["selected_bet_sha256"]),
        context_ref=normalized["context_ref"],
    )


def _validate_formal_session_store_payload(
    document: Mapping[str, Any],
    *,
    project_id: str,
    mission_id: str,
    session_id: str,
) -> Mapping[str, Any]:
    """Mechanically validate the exact successor Session Store payload."""

    normalized = _normalize_session(document)
    if canonical_json_bytes(normalized) != canonical_json_bytes(document):
        raise ValueError("formal Session Store payload is not canonical")
    if normalized["project_id"] != _text(project_id, "project_id"):
        raise ValueError("formal Session belongs to another project")
    if normalized["mission_id"] != _text(mission_id, "mission_id"):
        raise ValueError("formal Session belongs to another Mission")
    if normalized["session_id"] != _text(session_id, "session_id"):
        raise ValueError("formal Session payload has another canonical identity")
    return normalized


@dataclass(frozen=True, slots=True)
class FormalRequestSelection:
    """One exact Strategy bet that deliberately selects the formal lane."""

    strategy_ref: Mapping[str, Any]
    selected_bet_sha256: str
    selected_bet: Mapping[str, Any]

    def __post_init__(self) -> None:
        strategy_ref = _owner_ref(
            self.strategy_ref, "strategy_ref", required_kind="strategy"
        )
        selected_bet = _selected_bet(self.selected_bet)
        selected_bet_sha256 = _digest(self.selected_bet_sha256, "selected_bet_sha256")
        if _sha256(selected_bet) != selected_bet_sha256:
            raise ValueError("formal request selected bet digest is stale")
        object.__setattr__(self, "strategy_ref", strategy_ref)
        object.__setattr__(self, "selected_bet", selected_bet)

    @property
    def purpose(self) -> str:
        return str(self.selected_bet["formal_request"]["purpose"])

    @property
    def context_ref(self) -> Mapping[str, Any]:
        return self.selected_bet["formal_request"]["context_ref"]


def discover_formal_requests(
    strategy: PersistedStrategyRevision,
) -> tuple[FormalRequestSelection, ...]:
    """Return only bets whose Strategy meaning explicitly requests formal work."""

    if type(strategy) is not PersistedStrategyRevision:
        raise TypeError(
            "formal request discovery requires a persisted Strategy revision"
        )
    result = tuple(
        FormalRequestSelection(
            strategy_ref=strategy.to_reference(),
            selected_bet_sha256=_sha256(selected_bet),
            selected_bet=selected_bet,
        )
        for selected_bet in strategy.record.document["selected_bets"]
        if "formal_request" in selected_bet
    )
    digests = tuple(item.selected_bet_sha256 for item in result)
    if len(digests) != len(set(digests)):
        raise WorkspaceIntegrityError(
            "Strategy formal requests have a digest collision"
        )
    return tuple(sorted(result, key=lambda item: item.selected_bet_sha256))


@dataclass(frozen=True, slots=True)
class FormalSessionRevision:
    """One canonical schema-v1 formal Session owner document."""

    document: Mapping[str, Any]
    payload_sha256: str

    def __post_init__(self) -> None:
        normalized = _normalize_session(self.document)
        if canonical_json_bytes(normalized) != canonical_json_bytes(self.document):
            raise ValueError("formal Session document is not canonical")
        if self.payload_sha256 != _sha256(normalized):
            raise ValueError("formal Session payload digest is stale")
        object.__setattr__(self, "document", normalized)

    @property
    def semantic_identity_sha256(self) -> str:
        return _sha256(_semantic_identity(self.document))


def _session_record(document: Mapping[str, Any]) -> FormalSessionRevision:
    normalized = _normalize_session(document)
    return FormalSessionRevision(normalized, _sha256(normalized))


def _head_pair(revision: int | None, digest: str | None) -> None:
    if (revision is None) != (digest is None):
        raise ValueError("expected Session head revision and digest travel together")
    if revision is not None:
        _positive_int(revision, "expected Session head revision")
        _digest(digest, "expected Session head payload digest")


@dataclass(frozen=True, slots=True)
class PreparedFormalSessionRevision:
    """A Session revision fenced to the exact head observed during preparation."""

    record: FormalSessionRevision
    expected_head_revision: int | None
    expected_head_payload_digest: str | None

    def __post_init__(self) -> None:
        if type(self.record) is not FormalSessionRevision:
            raise TypeError("prepared Session requires an exact FormalSessionRevision")
        _head_pair(self.expected_head_revision, self.expected_head_payload_digest)
        if (
            self.expected_head_revision is None
            and self.record.document["lifecycle"] != "open"
        ):
            raise ValueError("a new formal Session must begin open")


@dataclass(frozen=True, slots=True)
class PersistedFormalSessionRevision:
    """One Store-owned formal Session revision and direct-owner metadata."""

    record: FormalSessionRevision
    revision: int
    predecessor_revision: int | None
    created_actor: str
    created_at: str
    is_current_head: bool

    def __post_init__(self) -> None:
        if type(self.record) is not FormalSessionRevision:
            raise TypeError("persisted Session requires an exact FormalSessionRevision")
        _positive_int(self.revision, "Session revision")
        if self.predecessor_revision is None:
            if self.revision != 1:
                raise ValueError("only the open Session revision lacks a predecessor")
        elif (
            _positive_int(self.predecessor_revision, "Session predecessor revision")
            != self.revision - 1
        ):
            raise ValueError("Session predecessor revision is not contiguous")
        lifecycle = self.record.document["lifecycle"]
        if (lifecycle, self.revision, self.predecessor_revision) not in {
            ("open", 1, None),
            ("terminal", 2, 1),
        }:
            raise ValueError("formal Session may advance only open to terminal")
        _text(self.created_actor, "Session created_actor")
        _text(self.created_at, "Session created_at")
        if not isinstance(self.is_current_head, bool):
            raise TypeError("Session current-head state must be boolean")

    def to_reference(self) -> Mapping[str, Any]:
        return deep_freeze(
            {
                "kind": "session",
                "identity": self.record.document["session_id"],
                "revision": self.revision,
                "payload_sha256": self.record.payload_sha256,
            }
        )


def _stored_session(stored: Mapping[str, Any]) -> PersistedFormalSessionRevision:
    if not isinstance(stored, Mapping) or set(stored) != _SESSION_STORE_KEYS:
        raise WorkspaceIntegrityError(
            "stored formal Session has the wrong closed shape"
        )
    record = _session_record(stored["payload"])
    if (
        stored["session_id"] != record.document["session_id"]
        or stored["mission_id"] != record.document["mission_id"]
        or stored["payload_digest"] != record.payload_sha256
    ):
        raise WorkspaceIntegrityError(
            "stored Session metadata disagrees with its payload"
        )
    return PersistedFormalSessionRevision(
        record=record,
        revision=int(stored["revision"]),
        predecessor_revision=(
            None
            if stored["predecessor_revision"] is None
            else int(stored["predecessor_revision"])
        ),
        created_actor=str(stored["created_actor"]),
        created_at=str(stored["created_at"]),
        is_current_head=True,
    )


def read_formal_session_revision(
    store: Any,
    *,
    mission_id: str,
    session_id: str,
    revision: int | None = None,
) -> PersistedFormalSessionRevision:
    """Read one exact current or historical successor formal Session revision."""

    mission_id = _text(mission_id, "mission_id")
    session_id = _text(session_id, "session_id")
    if revision is not None:
        _positive_int(revision, "Session revision")
    stored = store.read_formal_session_revision(
        mission_id=mission_id,
        session_id=session_id,
        revision=revision,
    )
    result = _stored_session(stored)
    if (
        result.record.document["mission_id"] != mission_id
        or result.record.document["session_id"] != session_id
    ):
        raise WorkspaceIntegrityError("Store returned another formal Session identity")
    if revision is not None:
        current = _stored_session(
            store.read_formal_session_revision(
                mission_id=mission_id,
                session_id=session_id,
                revision=None,
            )
        )
        object.__setattr__(
            result,
            "is_current_head",
            current.revision == result.revision
            and current.record.payload_sha256 == result.record.payload_sha256,
        )
    return result


def _assert_active_authority(
    store: Any,
    authority: DirectExecutiveEpochAuthority,
) -> Mapping[str, Any]:
    if type(authority) is not DirectExecutiveEpochAuthority:
        raise TypeError("formal Session requires direct Executive Epoch authority")
    authority.verify_integrity()
    metadata = store.read_metadata()
    event_readbacks = store.read_active_executive_epoch(authority.mission_id)
    if event_readbacks is None:
        raise StaleCommandError("formal Session requires one active Executive Epoch")
    current_authority = reissue_direct_executive_epoch_authority(
        event_readbacks=event_readbacks,
        project_id=_text(metadata["project_id"], "project_id"),
        root_identity=_text(metadata["root_identity"], "root_identity"),
        canonical_authority_digest=_digest(
            metadata["canonical_authority_digest"], "canonical_authority_digest"
        ),
    )
    if current_authority.authority_sha256 != authority.authority_sha256:
        raise StaleCommandError(
            "formal Session authority differs from the active Store epoch binding"
        )
    mission_ref = _owner_ref(
        authority.mission_root,
        "Executive Epoch Mission root",
        required_kind="mission",
    )
    mission_head = store.get_head(
        TypedWorkspaceId(IdentityKind.MISSION, authority.mission_id)
    )
    if mission_head is None or (
        mission_head.reference.revision,
        mission_head.payload_digest,
    ) != (mission_ref["revision"], mission_ref["payload_sha256"]):
        raise StaleCommandError("Executive Epoch Mission root is not the current head")
    return mission_ref


def _context_ref(context: PersistedContextRevision) -> Mapping[str, Any]:
    return deep_freeze(
        {
            "kind": "context",
            "identity": context.record.document["context_id"],
            "revision": context.revision,
            "payload_sha256": context.payload_digest,
        }
    )


def _current_formal_request(
    store: Any,
    *,
    authority: DirectExecutiveEpochAuthority,
    selected_bet_sha256: str,
) -> tuple[
    Mapping[str, Any],
    PersistedStrategyRevision,
    FormalRequestSelection,
    PersistedContextRevision,
]:
    mission_ref = _assert_active_authority(store, authority)
    strategy = read_mission_strategy_head(store, mission_id=authority.mission_id)
    if (
        strategy.record.document["project_id"] != authority.project_id
        or strategy.record.document["mission_id"] != authority.mission_id
    ):
        raise StaleCommandError("current Strategy belongs to another active Mission")
    selected_bet_sha256 = _digest(selected_bet_sha256, "selected_bet_sha256")
    matched = tuple(
        item
        for item in discover_formal_requests(strategy)
        if item.selected_bet_sha256 == selected_bet_sha256
    )
    if len(matched) != 1:
        raise StaleCommandError("selected formal request is absent or changed")
    selection = matched[0]
    context_ref = selection.context_ref
    context = read_context_revision(
        store,
        context_id=str(context_ref["identity"]),
    )
    if (
        not context.is_current_head
        or _context_ref(context) != context_ref
        or context.record.document["project_id"] != authority.project_id
        or context.record.document["mission_id"] != authority.mission_id
    ):
        raise StaleCommandError(
            "selected formal request Context is not the exact current Mission head"
        )
    if context.record.document["schema_version"] == 3:
        # Formal requests select one complete v1/v2 Context. They have no
        # scientific-treatment selection contract, so v3 cannot be expanded
        # implicitly into an Attempt package after committing its Session.
        raise StaleCommandError(
            "scientific Context v3 is unsupported for formal requests: "
            "no selected-treatment contract is defined"
        )
    return mission_ref, strategy, selection, context


def _existing_session_or_none(
    store: Any,
    *,
    mission_id: str,
    session_id: str,
) -> PersistedFormalSessionRevision | None:
    try:
        return read_formal_session_revision(
            store,
            mission_id=mission_id,
            session_id=session_id,
        )
    except StaleCommandError:
        return None


def prepare_formal_session_creation(
    store: Any,
    *,
    authority: DirectExecutiveEpochAuthority,
    formal_request: FormalRequestSelection,
) -> PreparedFormalSessionRevision:
    """Prepare one actual Strategy formal request as a first-class Session."""

    if type(formal_request) is not FormalRequestSelection:
        raise TypeError("Session creation requires an exact FormalRequestSelection")
    mission_ref = _assert_active_authority(store, authority)
    session_id = canonical_formal_session_id(
        project_id=authority.project_id,
        mission_ref=mission_ref,
        strategy_ref=formal_request.strategy_ref,
        selected_bet_sha256=formal_request.selected_bet_sha256,
        context_ref=formal_request.context_ref,
    )
    existing = _existing_session_or_none(
        store,
        mission_id=authority.mission_id,
        session_id=session_id,
    )
    requested_identity = _session_identity_material(
        project_id=authority.project_id,
        mission_ref=mission_ref,
        strategy_ref=formal_request.strategy_ref,
        selected_bet_sha256=formal_request.selected_bet_sha256,
        context_ref=formal_request.context_ref,
    )
    if existing is not None:
        if _semantic_identity(existing.record.document) != requested_identity:
            raise WorkspaceIntegrityError(
                "canonical formal Session identity has another semantic request"
            )
        return PreparedFormalSessionRevision(
            existing.record,
            existing.revision,
            existing.record.payload_sha256,
        )

    current_mission_ref, strategy, selection, _context = _current_formal_request(
        store,
        authority=authority,
        selected_bet_sha256=formal_request.selected_bet_sha256,
    )
    if (
        current_mission_ref != mission_ref
        or strategy.to_reference() != formal_request.strategy_ref
        or selection != formal_request
    ):
        raise StaleCommandError(
            "formal request is no longer the exact current Strategy request"
        )
    record = _session_record(
        {
            "schema_version": FORMAL_SESSION_SCHEMA_VERSION,
            "kind": "formal_session",
            "project_id": authority.project_id,
            "mission_id": authority.mission_id,
            "session_id": session_id,
            "lifecycle": "open",
            "mission_ref": mission_ref,
            "strategy_ref": strategy.to_reference(),
            "selected_bet_sha256": selection.selected_bet_sha256,
            "context_ref": selection.context_ref,
            "terminal_binding": None,
        }
    )
    return PreparedFormalSessionRevision(record, None, None)


def _dependency_heads(document: Mapping[str, Any]) -> Mapping[str, tuple[int, str]]:
    dependencies: dict[str, tuple[int, str]] = {}
    for key in ("mission_ref", "strategy_ref", "context_ref"):
        reference = _owner_ref(document[key], key)
        dependencies[f"{reference['kind']}:{reference['identity']}"] = (
            int(reference["revision"]),
            str(reference["payload_sha256"]),
        )
    return deep_freeze(dict(sorted(dependencies.items())))


def _verify_historical_request(
    store: Any,
    *,
    document: Mapping[str, Any],
) -> None:
    mission_ref = _owner_ref(
        document["mission_ref"], "mission_ref", required_kind="mission"
    )
    stored_mission = store.get_revision(
        RevisionRef(
            TypedWorkspaceId(IdentityKind.MISSION, str(mission_ref["identity"])),
            int(mission_ref["revision"]),
        )
    )
    if (
        stored_mission is None
        or stored_mission.payload_digest != mission_ref["payload_sha256"]
    ):
        raise WorkspaceIntegrityError("formal Session Mission revision is unavailable")
    strategy_ref = _owner_ref(
        document["strategy_ref"], "strategy_ref", required_kind="strategy"
    )
    strategy = read_strategy_revision(
        store,
        mission_id=str(document["mission_id"]),
        strategy_id=str(strategy_ref["identity"]),
        revision=int(strategy_ref["revision"]),
    )
    if strategy.to_reference() != strategy_ref:
        raise WorkspaceIntegrityError("formal Session Strategy revision is unavailable")
    matched = tuple(
        request
        for request in discover_formal_requests(strategy)
        if request.selected_bet_sha256 == document["selected_bet_sha256"]
    )
    if len(matched) != 1 or matched[0].context_ref != document["context_ref"]:
        raise WorkspaceIntegrityError(
            "formal Session request differs from its historical Strategy revision"
        )
    context_ref = _owner_ref(
        document["context_ref"], "context_ref", required_kind="context"
    )
    context = read_context_revision(
        store,
        context_id=str(context_ref["identity"]),
        revision=int(context_ref["revision"]),
    )
    if _context_ref(context) != context_ref:
        raise WorkspaceIntegrityError("formal Session Context revision is unavailable")


def _terminal_dependency_heads(
    store: Any,
    *,
    authority: DirectExecutiveEpochAuthority,
    document: Mapping[str, Any],
) -> Mapping[str, tuple[int, str]]:
    current_mission_ref = _assert_active_authority(store, authority)
    _verify_historical_request(store, document=document)
    return deep_freeze(
        {
            f"mission:{current_mission_ref['identity']}": (
                int(current_mission_ref["revision"]),
                str(current_mission_ref["payload_sha256"]),
            )
        }
    )


def _revalidate_request(
    store: Any,
    *,
    authority: DirectExecutiveEpochAuthority,
    document: Mapping[str, Any],
) -> Mapping[str, tuple[int, str]]:
    if (
        document["project_id"] != authority.project_id
        or document["mission_id"] != authority.mission_id
    ):
        raise ValueError("formal Session belongs to another active Mission")
    mission_ref, strategy, selection, context = _current_formal_request(
        store,
        authority=authority,
        selected_bet_sha256=str(document["selected_bet_sha256"]),
    )
    if (
        mission_ref != document["mission_ref"]
        or strategy.to_reference() != document["strategy_ref"]
        or selection.context_ref != document["context_ref"]
        or _context_ref(context) != document["context_ref"]
    ):
        raise StaleCommandError(
            "formal Session semantic request is no longer the exact current request"
        )
    return _dependency_heads(document)


def _assert_prepared_target_current(
    store: Any,
    *,
    prepared: PreparedFormalSessionRevision,
) -> None:
    document = prepared.record.document
    current = _existing_session_or_none(
        store,
        mission_id=str(document["mission_id"]),
        session_id=str(document["session_id"]),
    )
    if prepared.expected_head_revision is None:
        if current is not None:
            raise StaleCommandError(
                "formal Session identity appeared after preparation"
            )
        return
    if current is None or (
        current.revision,
        current.record.payload_sha256,
    ) != (
        prepared.expected_head_revision,
        prepared.expected_head_payload_digest,
    ):
        raise StaleCommandError("formal Session head changed after preparation")
    if _semantic_identity(current.record.document) != _semantic_identity(document):
        raise WorkspaceIntegrityError("formal Session semantic identity changed")
    if (
        current.record.document["lifecycle"] == "terminal"
        and current.record.payload_sha256 != prepared.record.payload_sha256
    ):
        raise ValueError("terminal formal Session cannot be revised again")


def _commit_formal_session(
    store: Any,
    *,
    authority: DirectExecutiveEpochAuthority,
    prepared: PreparedFormalSessionRevision,
    lease: Any,
    actor: str,
    command_id: str | None,
) -> Any:
    if type(prepared) is not PreparedFormalSessionRevision:
        raise TypeError("formal Session commit requires PreparedFormalSessionRevision")
    document = prepared.record.document
    _assert_prepared_target_current(store, prepared=prepared)
    dependency_heads = (
        _revalidate_request(
            store,
            authority=authority,
            document=document,
        )
        if (document["lifecycle"] == "open" and prepared.expected_head_revision is None)
        else _terminal_dependency_heads(
            store,
            authority=authority,
            document=document,
        )
    )
    return store.commit_formal_session_revision(
        executive_epoch_id=authority.executive_epoch_id,
        mission_id=str(document["mission_id"]),
        session_id=str(document["session_id"]),
        payload=document,
        expected_head_revision=prepared.expected_head_revision,
        expected_head_payload_digest=prepared.expected_head_payload_digest,
        dependency_heads=dependency_heads,
        lease=lease,
        command_id=(
            command_id
            or f"formal-session:{document['session_id']}:{prepared.record.payload_sha256}"
        ),
        actor=_text(actor, "Session actor"),
        expected_canonical_authority_digest=authority.canonical_authority_digest,
    )


def commit_formal_session_creation(
    store: Any,
    *,
    authority: DirectExecutiveEpochAuthority,
    prepared: PreparedFormalSessionRevision,
    lease: Any,
    actor: str,
    command_id: str | None = None,
) -> Any:
    """Commit or exactly replay one prepared Session creation."""

    if type(prepared) is not PreparedFormalSessionRevision:
        raise TypeError("Session creation requires PreparedFormalSessionRevision")
    if (
        prepared.expected_head_revision is not None
        and prepared.record.document["lifecycle"] == "terminal"
    ):
        # An exact creation replay after terminalization must remain terminal.
        pass
    elif prepared.record.document["lifecycle"] != "open":
        raise ValueError("Session creation may only create an open Session")
    return _commit_formal_session(
        store,
        authority=authority,
        prepared=prepared,
        lease=lease,
        actor=actor,
        command_id=command_id,
    )


def _verify_raw_capture_binding(
    store: Any,
    *,
    mission_id: str,
    session_id: str,
    raw_capture_ref: Mapping[str, Any],
) -> None:
    capture = read_raw_capture(
        store,
        capture_id=str(raw_capture_ref["capture_id"]),
    )
    if (
        capture.digest_sha256 != raw_capture_ref["capture_digest_sha256"]
        or capture.mission_id != mission_id
        or capture.capture_kind != "output"
        or capture.assignment_id != session_id
    ):
        raise ValueError("raw capture is not the exact formal Session output custody")


def prepare_formal_session_terminal_revision(
    store: Any,
    *,
    authority: DirectExecutiveEpochAuthority,
    verified_result: VerifiedFormalSessionResult,
) -> PreparedFormalSessionRevision:
    """Prepare the sole open-to-terminal Session transition."""

    if type(verified_result) is not VerifiedFormalSessionResult:
        raise TypeError(
            "Session terminalization requires a bridge-issued VerifiedFormalSessionResult"
        )
    verified_result.verify_issued()
    session_id = verified_result.session_id
    current = read_formal_session_revision(
        store,
        mission_id=authority.mission_id,
        session_id=session_id,
    )
    _terminal_dependency_heads(
        store,
        authority=authority,
        document=current.record.document,
    )
    capture_ref = verified_result.raw_capture_ref
    if capture_ref is not None:
        _verify_raw_capture_binding(
            store,
            mission_id=authority.mission_id,
            session_id=session_id,
            raw_capture_ref=capture_ref,
        )
    requested_binding = verified_result.terminal_binding
    if current.record.document["lifecycle"] == "terminal":
        if current.record.document["terminal_binding"] != requested_binding:
            raise ValueError(
                "formal Session is already terminal with another result binding"
            )
        return PreparedFormalSessionRevision(
            current.record,
            current.revision,
            current.record.payload_sha256,
        )
    terminal_document = deep_thaw(current.record.document)
    terminal_document["lifecycle"] = "terminal"
    terminal_document["terminal_binding"] = requested_binding
    terminal = _session_record(terminal_document)
    if _semantic_identity(terminal.document) != _semantic_identity(
        current.record.document
    ):
        raise WorkspaceIntegrityError(
            "formal Session terminalization changed semantic request identity"
        )
    return PreparedFormalSessionRevision(
        terminal,
        current.revision,
        current.record.payload_sha256,
    )


def commit_formal_session_terminal_revision(
    store: Any,
    *,
    authority: DirectExecutiveEpochAuthority,
    prepared: PreparedFormalSessionRevision,
    lease: Any,
    actor: str,
    command_id: str | None = None,
) -> Any:
    """Commit or exactly replay the sole terminal Session binding."""

    if type(prepared) is not PreparedFormalSessionRevision:
        raise TypeError(
            "Session terminal commit requires PreparedFormalSessionRevision"
        )
    document = prepared.record.document
    if document["lifecycle"] != "terminal":
        raise ValueError("Session terminal commit requires a terminal revision")
    binding = document["terminal_binding"]
    assert binding is not None
    if binding["raw_capture_ref"] is not None:
        _verify_raw_capture_binding(
            store,
            mission_id=str(document["mission_id"]),
            session_id=str(document["session_id"]),
            raw_capture_ref=binding["raw_capture_ref"],
        )
    return _commit_formal_session(
        store,
        authority=authority,
        prepared=prepared,
        lease=lease,
        actor=actor,
        command_id=command_id,
    )


def terminate_formal_session(
    store: Any,
    *,
    authority: DirectExecutiveEpochAuthority,
    verified_result: VerifiedFormalSessionResult,
    lease: Any,
    actor: str,
    command_id: str | None = None,
) -> Any:
    """Prepare and commit the exact terminal result/capture binding once."""

    prepared = prepare_formal_session_terminal_revision(
        store,
        authority=authority,
        verified_result=verified_result,
    )
    return commit_formal_session_terminal_revision(
        store,
        authority=authority,
        prepared=prepared,
        lease=lease,
        actor=actor,
        command_id=command_id,
    )
