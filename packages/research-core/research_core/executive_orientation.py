"""Ephemeral, owner-derived Executive orientation; never a research writer.

The ``*_in_snapshot`` functions require the caller's existing Store read scope.
They do not call reconstruction, recursively expand owners, or parse quoted
mathematical text. Exact reads and source corrections use explicit projections.
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any, Mapping

from .candidate_a1_triage import CandidateA1Ref
from .canonical_snapshot import (
    CANONICAL_STATE_REPO_PATH,
    load_canonical_snapshot,
    verify_snapshot_current,
)
from .complete_claim_admission import (
    AdmissionEvidenceRef,
    admission_case_evidence_id,
    admission_decision_evidence_id,
    admission_review_evidence_id,
)
from .context_revision import (
    _validate_first_class_context_document,
)
from .formal_session import (
    canonical_formal_session_id,
    discover_formal_requests,
    read_formal_session_revision,
)
from .mission_frontier import (
    PersistedStrategyRevision,
    read_strategy_revision,
)
from .json_support import canonical_json_bytes
from .research_model import deep_thaw
from .workspace_schema import IdentityKind, TypedWorkspaceId
from .workspace_store import (
    StaleCommandError,
    WorkspaceIntegrityError,
    _validated_current_canonical_authority_json,
)


ORIENTATION_SCHEMA = "mathematical_research.executive_orientation.v3"
HOST_SNAPSHOT_SCHEMA = "mathematical_research.mission_host_snapshot.v4"
_REF_KEYS = {"kind", "identity", "revision", "payload_sha256"}
_STRATEGY_FIELDS = (
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
)


def current_strategy_in_snapshot(
    service: Any,
    mission_owner: Mapping[str, Any],
) -> PersistedStrategyRevision:
    """Read the one Mission-selected current Strategy by exact indexed identity."""

    document = mission_owner.get("document")
    strategy_ids = (
        None if not isinstance(document, Mapping) else document.get("strategy_ids")
    )
    if (
        not isinstance(strategy_ids, (list, tuple))
        or len(strategy_ids) != 1
        or not isinstance(strategy_ids[0], str)
        or not strategy_ids[0].strip()
    ):
        raise WorkspaceIntegrityError(
            "current Mission does not bind one exact Strategy identity"
        )
    return read_strategy_revision(
        service._store,
        mission_id=service._mission_id,
        strategy_id=strategy_ids[0],
    )
_BRANCH_FIELDS = (
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
)
_CONTEXT_READ_FIELDS = (
    "purpose",
    "question",
    "indispensable_ground",
    "owner_source_references",
    "known_omissions",
    "restricted_uses",
    "independence_treatment",
    "restrictions",
    "invalidation_conditions",
    "immutable_historical_references",
    "untrusted_material_locators",
    "historical_advisory_scope",
)
_SCIENTIFIC_CONTEXT_FIELDS = (
    "purpose", "question", "treatments", "exposed_treatments",
    "known_omissions", "restricted_uses", "restrictions", "independence_treatment",
)
_SCIENTIFIC_CONTEXT_QUALIFICATION_FIELDS = (
    "known_omissions", "restricted_uses", "restrictions", "independence_treatment",
)
_CANDIDATE_FIELDS = (
    "proposal_kind",
    "exact_statement",
    "standing",
    "mechanism",
    "scope_and_reach",
    "complete_target_claim",
    "objects",
    "hypotheses",
    "domain",
    "quantifiers",
    "normalization",
    "argument_edges",
    "supporting_refs",
    "obligations",
    "gaps",
    "objections",
    "circularity_risks",
    "falsifiers",
    "discriminators",
    "limitations",
    "non_inferences",
    "genealogy",
    # Retained v1 Candidates have a different, still-readable semantic contract.
    "lifecycle",
    "claimed_scope",
    "normalizations",
    "argument_steps",
    "dependencies",
    "component_evidence_refs",
    "smallest_falsifier",
    "full_rh_case",
)
_CANDIDATE_QUALIFICATION_FIELDS = tuple(
    field
    for field in _CANDIDATE_FIELDS
    if field not in {"argument_edges", "argument_steps"}
)
_EVIDENCE_FIELDS = (
    "subtype",
    "subject",
    "exact_scope",
    "rigor",
    "limitations",
    "non_inferences",
)
_RESERVED_EVIDENCE_SUBJECT_FIELDS = {
    "candidate_a1_triage": (
        "candidate_ref",
        "disposition",
        "review_finding",
        "cited_basis",
        "concrete_defects",
        "no_remaining_material_objection",
        "limitations",
        "non_inferences",
    ),
    "complete_claim_admission_case": (
        "candidate_ref",
        "triage_ref",
        "candidate_disposition",
        "exact_claim",
        "source_closure",
    ),
    "complete_claim_admission_review": (
        "candidate_ref",
        "case_ref",
        "disposition",
        "review_finding",
        "objections",
        "cited_basis",
    ),
    "complete_claim_admission_decision": (
        "candidate_ref",
        "case_ref",
        "review_ref",
        "disposition",
        "decision_basis",
        "objections",
        "cited_basis",
    ),
}
_EVIDENCE_QUALIFICATION_SUBJECT_FIELDS = {
    "evidence_meaning": ("statement", "semantic_role", "decision_consequence"),
    # The unverified artifact remains in CAS; routing and digest facts are private.
    "purported_complete_route": ("kind", "candidate_id", "claimed_scope"),
    **_RESERVED_EVIDENCE_SUBJECT_FIELDS,
}
_ROLES = {
    "causal_inputs": "causal_input",
    "serious_opportunities": "serious_opportunity",
    "selected_bets": "selected_bet_ground",
    "attention_actions": "branch_attention",
    "context_treatment": "context_treatment",
    "creativity_treatment": "creativity_ground",
    "reconsideration_conditions": "reconsideration_ground",
    "reversal_conditions": "reversal_ground",
    "revival_conditions": "revival_ground",
    "owner_refs": "strategy_owner_ref",
}
ROOT_RETRIEVAL_MODES = (
    "read",
    "search",
    "inventory",
    "checkpoint",
    "changes_since_checkpoint",
    "hooks",
    "captures",
    "proof_attention",
)


def reference_key(reference: Mapping[str, Any]) -> tuple[str, str, int, str]:
    return (
        str(reference["kind"]),
        str(reference["identity"]),
        int(reference["revision"]),
        str(reference["payload_sha256"]),
    )


def retrieval_handle(reference: Mapping[str, Any]) -> str:
    return f"{reference['kind']}:{reference['identity']}@{reference['revision']}"


def _selector(reference: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "id": f"{reference['kind']}:{reference['identity']}",
        "revision": int(reference["revision"]),
    }


def _references(value: Any) -> Any:
    """Project known reference-bearing fields, preserving all text literally."""
    if isinstance(value, Mapping):
        if set(value) in (_REF_KEYS, _REF_KEYS | {"selection"}):
            return {
                **_selector(value),
                **({"selection": deep_thaw(value["selection"])} if "selection" in value else {}),
            }
        if set(value) in (
            {"kind", "id", "revision", "digest_sha256"},
            {"kind", "id", "revision", "digest_sha256", "selection"},
        ):
            return {
                "id": f"{value['kind']}:{value['id']}",
                "revision": value["revision"],
                **({"selection": deep_thaw(value["selection"])} if "selection" in value else {}),
            }
        if set(value) == {"candidate_id", "revision", "digest_sha256"}:
            return {
                "id": f"candidate:{value['candidate_id']}",
                "revision": value["revision"],
            }
        if set(value) == {"mission_id", "candidate_id", "revision", "digest_sha256"}:
            return {
                "id": f"candidate:{value['candidate_id']}",
                "revision": value["revision"],
            }
        if set(value) in (
            {"evidence_id", "revision", "payload_sha256"},
            {"mission_id", "evidence_id", "revision", "digest_sha256"},
        ):
            return {
                "id": f"evidence:{value['evidence_id']}",
                "revision": value["revision"],
            }
        return {str(key): _references(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_references(item) for item in value]
    return value


def semantic_owner_document(kind: str, document: Mapping[str, Any]) -> dict[str, Any]:
    """Explicit exact read projection, not an authority-field-name scrubber.

    Free-form semantic objects and strings are copied, never recursively sanitized.
    Only typed reference fields are converted into digestless selection handles.
    """
    fields: tuple[str, ...]
    refs: set[str]
    if kind == "mission":
        fields = (
            "lifecycle", "effective", "autonomous", "purpose", "scientific_context_id"
        )
        refs = set()
    elif kind == "strategy":
        fields, refs = _STRATEGY_FIELDS, set(_ROLES)
    elif kind == "branch":
        fields, refs = (
            _BRANCH_FIELDS,
            set(_BRANCH_FIELDS)
            - {"question", "leverage_fingerprint", "target_hook", "nonclaims"},
        )
    elif kind == "context":
        fields = (
            _SCIENTIFIC_CONTEXT_FIELDS
            if document.get("schema_version") == 3 or "treatments" in document
            else _CONTEXT_READ_FIELDS
        )
        refs = {
            "indispensable_ground",
            "invalidation_conditions",
            "immutable_historical_references",
        }
    elif kind == "candidate":
        fields, refs = (
            _CANDIDATE_FIELDS,
            {
                "supporting_refs",
                "genealogy",
                "argument_edges",
                "argument_steps",
                "dependencies",
                "component_evidence_refs",
            },
        )
    elif kind == "evidence":
        fields = _EVIDENCE_FIELDS
        refs = set()
    elif kind == "session":
        fields = (
            "lifecycle",
            "mission_ref",
            "strategy_ref",
            "selected_bet_sha256",
            "selected_bet",
            "context_ref",
            "terminal_binding",
        )
        refs = {"mission_ref", "strategy_ref", "selected_bet", "context_ref"}
    elif kind == "capture-annotation":
        fields = (
            "annotation_id",
            "capture_id",
            "exact_scope",
            "lifecycle",
            "annotation_kind",
        )
        refs = set()
    else:
        raise ValueError(f"unsupported semantic owner kind {kind!r}")
    projected = {
        field: (
            _references(document[field])
            if field in refs
            else deep_thaw(document[field])
        )
        for field in fields
        if field in document
    }
    if kind == "mission":
        projected.setdefault("scientific_context_id", None)
    if kind == "context" and "owner_source_references" in document:
        projected["owner_source_references"] = [
            {
                "reference": _references(item["reference"]),
                "retrieval": item["retrieval"],
            }
            for item in document["owner_source_references"]
        ]
    if kind == "evidence" and isinstance(document.get("subject"), Mapping):
        # Subject mathematical prose/objects remain literal; these are the
        # explicit Mission Evidence envelope fields, not recursive deny-keys.
        subject = document["subject"]
        reserved_fields = _RESERVED_EVIDENCE_SUBJECT_FIELDS.get(
            str(document.get("subtype"))
        )
        typed_refs = {
            "dependencies",
            "owner_refs",
            "inputs",
            "interpreted_owner_inputs",
            "candidate_ref",
            "triage_ref",
            "case_ref",
            "review_ref",
            "source_closure",
            "cited_basis",
        }
        projected["subject"] = {
            key: (_references(value) if key in typed_refs else deep_thaw(value))
            for key, value in subject.items()
            if (
                key in reserved_fields
                if reserved_fields is not None
                else key
                not in {"mission_id", "executive_epoch_id", "source_command_id"}
            )
        }
    if kind == "session" and document.get("terminal_binding") is not None:
        binding = document["terminal_binding"]
        attempt = binding["attempt_result_ref"]
        capture = binding["raw_capture_ref"]
        projected["terminal_binding"] = {
            "attempt_id": attempt["attempt_id"],
            "attempt_state": attempt["attempt_state"],
            "raw_capture_handle": None
            if capture is None
            else f"capture:{capture['capture_id']}",
        }
    return projected


def _non_context_source_qualification_document(
    kind: str, document: Mapping[str, Any]
) -> dict[str, Any]:
    """Project literal source qualifications without expanding referenced owners."""

    if kind == "context":
        raise ValueError("Context source qualifications require an exact selection")
    if kind == "candidate":
        document = {
            field: document[field]
            for field in _CANDIDATE_QUALIFICATION_FIELDS
            if field in document
        }
    elif kind == "evidence":
        fields = _EVIDENCE_QUALIFICATION_SUBJECT_FIELDS.get(str(document.get("subtype")))
        subject = document.get("subject")
        if fields is None or not isinstance(subject, Mapping):
            raise WorkspaceIntegrityError(
                "executive_orientation_evidence_subtype_invalid"
            )
        document = {
            field: (
                {key: subject[key] for key in fields if key in subject}
                if field == "subject"
                else document[field]
            )
            for field in _EVIDENCE_FIELDS
            if field in document
        }
    return semantic_owner_document(kind, document)


def strategy_connections(
    document: Mapping[str, Any],
) -> dict[tuple[str, str, int, str], list[dict[str, str]]]:
    result: dict[tuple[str, str, int, str], list[dict[str, str]]] = {}

    def visit(value: Any, pointer: str, role: str) -> None:
        if isinstance(value, Mapping):
            if set(value) == _REF_KEYS:
                result.setdefault(reference_key(value), []).append(
                    {"json_pointer": pointer, "role": role}
                )
                return
            for key, item in value.items():
                escaped = str(key).replace("~", "~0").replace("/", "~1")
                visit(
                    item,
                    f"{pointer}/{escaped}",
                    "formal_context" if key == "formal_request" else role,
                )
        elif isinstance(value, (list, tuple)):
            for index, item in enumerate(value):
                visit(item, f"{pointer}/{index}", role)

    for field, role in _ROLES.items():
        if field in document:
            visit(document[field], f"/{field}", role)
    return result


def _snapshot_facts(service: Any) -> tuple[Any, Mapping[str, Any]]:
    checkpoint = service._store.read_continuation_checkpoint(
        latest_mission_id=service._mission_id
    )
    cut = None if checkpoint is None else int(checkpoint["project_commit_no"])
    return checkpoint, service._store.read_direct_recovery_facts(cut_project_commit=cut)


def _admission_reference(store: Any, evidence_id: str) -> AdmissionEvidenceRef | None:
    try:
        row = store.read_evidence_meaning_revision(evidence_id, 1)
    except StaleCommandError:
        return None
    return AdmissionEvidenceRef(evidence_id, 1, str(row["payload_digest"]))


def _admission_handle(reference: AdmissionEvidenceRef) -> dict[str, Any]:
    return {
        "id": f"evidence:{reference.evidence_id}",
        "revision": reference.revision,
        "retrieval_handle": f"evidence:{reference.evidence_id}@{reference.revision}",
    }


def proof_attention_in_snapshot(
    service: Any, *, canonical_snapshot: Any = None
) -> dict[str, Any]:
    admitted = deep_thaw(service._store.read_admitted_result(service._mission_id))
    _canonical_status(
        canonical_snapshot_in_snapshot(service)
        if canonical_snapshot is None
        else canonical_snapshot,
        admitted,
    )
    items: list[dict[str, Any]] = []
    for binding in service._store._list_open_mission_candidate_a1_bindings(
        service._mission_id
    ):
        existing = service._candidate_a1_projection(
            binding, bounded_current=True
        )
        candidate_ref = CandidateA1Ref(
            service._mission_id,
            binding.candidate_id,
            binding.candidate_revision,
            binding.candidate_digest,
        )
        item: dict[str, Any] = {
            "candidate_ref": {
                "id": f"candidate:{binding.candidate_id}",
                "revision": binding.candidate_revision,
                "payload_sha256": binding.candidate_digest,
            },
            "retrieval_handle": existing["retrieval_handle"],
            "claim_disposition": existing["disposition"],
            "stage": "awaiting_a1_review",
            "triage": None,
            "admission": None,
        }
        if binding.triage_disposition is not None:
            triage = existing["triage"]
            item["triage"] = {
                "disposition": triage["disposition"],
                "evidence_ref": _selector(triage["evidence_ref"]),
                "retrieval_handle": triage["retrieval_handle"],
            }
            if binding.triage_disposition != "admission_ready":
                raise WorkspaceIntegrityError(
                    "OPEN A1 has a resolving triage disposition"
                )
            item["stage"] = "awaiting_admission_case"
            case_ref = _admission_reference(
                service._store,
                admission_case_evidence_id(service._store.project_id, candidate_ref),
            )
            if case_ref is not None:
                case = service._store.read_complete_claim_admission_case(case_ref)
                if case.candidate_ref != candidate_ref:
                    raise WorkspaceIntegrityError(
                        "Admission Case differs from exact OPEN A1"
                    )
                item["admission"] = {
                    "case": _admission_handle(case_ref),
                    "review": None,
                    "decision": None,
                }
                item["stage"] = "awaiting_admission_review"
                review_ref = _admission_reference(
                    service._store, admission_review_evidence_id(case_ref)
                )
                if review_ref is not None:
                    review = service._store.read_complete_claim_admission_review(
                        review_ref
                    )
                    item["admission"]["review"] = {
                        **_admission_handle(review_ref),
                        "disposition": review.disposition,
                    }
                    item["stage"] = "awaiting_admission_decision"
                    decision_ref = _admission_reference(
                        service._store,
                        admission_decision_evidence_id(case_ref, review_ref),
                    )
                    if decision_ref is not None:
                        decision = (
                            service._store.read_complete_claim_admission_decision(
                                decision_ref
                            )
                        )
                        item["admission"]["decision"] = {
                            **_admission_handle(decision_ref),
                            "disposition": decision.disposition,
                        }
                        if decision.disposition != "authorize_exact_delta":
                            raise WorkspaceIntegrityError(
                                "OPEN A1 has a resolving Admission Decision"
                            )
                        item["stage"] = "awaiting_external_canonical_rebind"
        items.append(item)
    items.sort(
        key=lambda item: (
            item["candidate_ref"]["id"],
            item["candidate_ref"]["revision"],
            item["candidate_ref"]["payload_sha256"],
        )
    )
    return {"open_candidate_a1": items, "admitted_result": admitted}


def _formal_requests(strategy: Any) -> list[dict[str, Any]]:
    return [
        {
            "selected_bet_sha256": selection.selected_bet_sha256,
            "bet": selection.selected_bet["bet"],
            "discriminator": selection.selected_bet["discriminator"],
            "purpose": selection.purpose,
            "context_retrieval_handle": retrieval_handle(selection.context_ref),
        }
        for selection in discover_formal_requests(strategy)
    ]


def formal_attention_in_snapshot(
    service: Any, mission_ref: Mapping[str, Any], strategy: Any
) -> list[dict[str, Any]]:
    result = []
    for selection, request in zip(
        discover_formal_requests(strategy), _formal_requests(strategy)
    ):
        session_id = canonical_formal_session_id(
            project_id=service._store.project_id,
            mission_ref=mission_ref,
            strategy_ref=strategy.to_reference(),
            selected_bet_sha256=selection.selected_bet_sha256,
            context_ref=selection.context_ref,
        )
        head = service._store.get_head(
            TypedWorkspaceId(IdentityKind.SESSION, session_id)
        )
        item = {
            **request,
            "session_state": "not_started",
            "session_handle": None,
            "terminal": None,
        }
        if head is not None:
            session = read_formal_session_revision(
                service._store,
                mission_id=service._mission_id,
                session_id=session_id,
                revision=head.reference.revision,
            )
            document = session.record.document
            item["session_state"] = str(document["lifecycle"])
            item["session_handle"] = f"session:{session_id}@{session.revision}"
            item["terminal"] = semantic_owner_document("session", document).get(
                "terminal_binding"
            )
        result.append(item)
    return result


def failed_output_is_relevant(
    *,
    capture_kind: str,
    origin_commit: int,
    terminal_kind: str | None,
    terminal_commit: int | None,
    baseline_commit: int | None,
    pending: bool,
) -> bool:
    baseline = 0 if baseline_commit is None else baseline_commit
    return (
        capture_kind == "output"
        and pending
        and terminal_kind == "failed_before_checkpoint"
        and terminal_commit is not None
        and (terminal_commit > baseline or origin_commit > baseline)
    )


def capture_descriptors_in_snapshot(
    service: Any, facts: Mapping[str, Any] | None = None
) -> list[dict[str, Any]]:
    """Current-cut metadata only; historical page selection must derive its own cut."""
    if facts is None:
        _, facts = _snapshot_facts(service)
    pending = {
        (str(item["capture_id"]), int(item["artifact_ordinal"]))
        for item in service._pending_capture_locators()
    }
    origins = {
        str(item["capture_id"]): item
        for item in facts["raw_capture_origins"]
        if item["mission_id"] == service._mission_id
    }
    terminals = {
        str(item["executive_epoch_id"]): item
        for item in facts["terminal_epochs"]
        if item["mission_id"] == service._mission_id
    }
    result = []
    for row in service._store.list_mission_raw_captures(service._mission_id):
        document = row["record"]
        capture_id = str(document["capture_id"])
        origin = origins[capture_id]
        terminal = terminals.get(str(document["executive_epoch_id"]))
        terminal_kind = None if terminal is None else str(terminal["terminal_kind"])
        terminal_commit = None if terminal is None else int(terminal["project_commit"])
        artifacts = []
        for artifact in row["artifacts"]:
            ordinal = int(artifact["ordinal"])
            is_pending = (capture_id, ordinal) in pending
            artifacts.append(
                {
                    "ordinal": ordinal,
                    "role": artifact["role"],
                    "logical_name": artifact["logical_name"],
                    "retrieval_handle": f"capture-artifact:{capture_id}#{ordinal}",
                    "pending_current": is_pending,
                    "failed_interval_or_late_output": failed_output_is_relevant(
                        capture_kind=str(document["capture_kind"]),
                        origin_commit=int(origin["project_commit"]),
                        terminal_kind=terminal_kind,
                        terminal_commit=terminal_commit,
                        baseline_commit=facts["cut_project_commit"],
                        pending=is_pending,
                    ),
                }
            )
        result.append(
            {
                "capture_id": capture_id,
                "capture_kind": document["capture_kind"],
                "executive_epoch_id": document["executive_epoch_id"],
                "project_commit": int(origin["project_commit"]),
                "retrieval_handle": f"capture:{capture_id}",
                "artifacts": artifacts,
            }
        )
    if {item["capture_id"] for item in result} != set(origins):
        raise WorkspaceIntegrityError("Capture descriptors differ from journal origins")
    return result


def _call(mode: str, purpose: str, **selection: Any) -> dict[str, Any]:
    return {
        "operation": "retrieve",
        "input": {"mode": mode, "purpose": purpose, **selection},
    }


def recovery_facts_in_snapshot(
    service: Any, facts: Mapping[str, Any] | None = None
) -> dict[str, Any]:
    if facts is None:
        _, facts = _snapshot_facts(service)
    captures = capture_descriptors_in_snapshot(service, facts)
    affected = [
        item
        for item in captures
        if any(
            artifact["failed_interval_or_late_output"] for artifact in item["artifacts"]
        )
    ]
    origins = {item["executive_epoch_id"] for item in affected}
    baseline = (
        0 if facts["cut_project_commit"] is None else int(facts["cut_project_commit"])
    )
    terminal_epochs = []
    for terminal in facts["terminal_epochs"]:
        if terminal["mission_id"] != service._mission_id:
            continue
        if (
            int(terminal["project_commit"]) <= baseline
            and terminal["executive_epoch_id"] not in origins
        ):
            continue
        chain = service._store.read_executive_epoch_events(
            executive_epoch_id=str(terminal["executive_epoch_id"]),
            mission_id=service._mission_id,
        )
        latest = chain[-1]
        if latest["event_kind"] != terminal["terminal_kind"] or int(
            latest["project_commit_no"]
        ) != int(terminal["project_commit"]):
            raise WorkspaceIntegrityError(
                "Epoch recovery fact differs from validated terminal chain"
            )
        reconciliation = None
        if terminal["terminal_kind"] == "failed_before_checkpoint":
            raw = latest["event"]["reconciliation"]
            reconciliation = {
                "stage": raw["stage"],
                "failure_reason": _safe_failure_reason(str(raw["failure_reason"])),
            }
        terminal_epochs.append(
            {
                "executive_epoch_id": terminal["executive_epoch_id"],
                "terminal_kind": terminal["terminal_kind"],
                "project_commit": int(terminal["project_commit"]),
                "reconciliation": reconciliation,
            }
        )
    terminal_epochs.sort(
        key=lambda item: (item["project_commit"], item["executive_epoch_id"])
    )
    artifact_count = sum(
        artifact["failed_interval_or_late_output"]
        for item in affected
        for artifact in item["artifacts"]
    )
    return {
        "checkpoint_project_commit": facts["cut_project_commit"],
        "observed_project_commit": facts["observed_project_commit"],
        "terminal_epochs": terminal_epochs,
        "failed_output_attention": {
            "state": "present" if affected else "none",
            "capture_count": len(affected),
            "artifact_count": artifact_count,
            "retrieve_call": _call(
                "captures",
                "Inspect retained output relevant to failed-epoch continuity.",
                view="failed_interval_or_late_output",
                page_size=25,
            )
            if affected
            else None,
        },
    }


def _safe_failure_reason(value: str) -> str:
    # The persisted legacy field is arbitrary text. Never reflect that text into
    # compact operational telemetry; expand only for exact known Host codes.
    known = {
        "owner_checkpoint",
        "usage_limited",
        "operator_stop",
        "explicit_force_stop",
        "boundary_shutdown",
        "turn_start_failure",
        "model_contract_violation",
        "boundary_failure",
        "shared_authority_loss",
        "unknown_effect",
        "mission_consistency_failure",
        "dead_runner_recovery",
        "usageLimited",
        "host_state_missing",
    }
    return value if value in known else "unclassified_failure"


def canonical_snapshot_in_snapshot(service: Any) -> Any:
    """Bind the existing validated live canonical reader to this exact Store cut.

    Candidate inspection supplies only its trusted selected-release byte root.
    The authority source commit is read from persisted Workspace metadata, never
    manufactured from the selected runtime or candidate implementation release.
    """
    metadata = service._store.read_metadata()
    authority_json = metadata["canonical_authority_json"]
    authority = _validated_current_canonical_authority_json(authority_json)
    authority_raw = authority_json.encode("utf-8")
    if hashlib.sha256(authority_raw).hexdigest() != metadata["canonical_authority_digest"]:
        raise WorkspaceIntegrityError("executive_orientation_canonical_binding_mismatch")
    snapshot = service._canonical_snapshot
    if snapshot is None:
        root = service._canonical_repo_root
        if root is None:
            root = Path(__file__).resolve().parents[3]
        path = root / CANONICAL_STATE_REPO_PATH
        # Do not let path resolution silently follow a substituted canonical file
        # or directory outside the already selected immutable physical release.
        if (
            not root.is_absolute()
            or root != root.resolve()
            or path.resolve() != path
            or any(
                item.is_symlink() or getattr(item, "is_junction", lambda: False)()
                for item in (path, *path.parents)
            )
            or not path.is_file()
        ):
            raise WorkspaceIntegrityError("executive_orientation_canonical_source_invalid")
        loaded = load_canonical_snapshot(
            root,
            source_commit=authority["source_commit"],
        )
        if not loaded.ok or loaded.value is None:
            raise WorkspaceIntegrityError("executive_orientation_canonical_unavailable")
        snapshot = loaded.value
    if (
        canonical_json_bytes(snapshot.authority_vector.to_mapping()) != authority_raw
        or verify_snapshot_current(snapshot) is not None
    ):
        raise WorkspaceIntegrityError(
            "executive_orientation_canonical_binding_mismatch"
        )
    return snapshot


def _canonical_status(snapshot: Any, admitted: Any) -> str:
    project = snapshot.state["project"]
    status = project["proof_status"]
    if status == "incomplete":
        if admitted is not None or project.get("admitted_result") is not None:
            raise WorkspaceIntegrityError(
                "executive_orientation_canonical_result_mismatch"
            )
        return "open"
    if (
        status not in {"proved", "disproved"}
        or admitted is None
        or deep_thaw(project.get("admitted_result")) != admitted
        or admitted["disposition"] != status
    ):
        raise WorkspaceIntegrityError("executive_orientation_canonical_result_mismatch")
    return str(status)


def _relation(reference: Mapping[str, Any], current: Mapping[str, Any]) -> str:
    if (reference["kind"], reference["identity"]) != (
        current["kind"],
        current["identity"],
    ):
        return "different_lineage"
    return (
        "same_revision"
        if reference_key(reference) == reference_key(current)
        else "advanced_same_identity"
    )


def source_qualification_document(
    kind: str,
    document: Mapping[str, Any],
    selection: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Literal owner qualifications, not recursive source or exposure expansion.

    Context selection happens before copying treatment bodies. Reading and
    authenticating its immutable owner still costs the complete Context payload;
    the selected projection does not pretend that cost is Delta-local storage.
    Missing selected keys are reported separately by the scientific projection.
    """
    if kind == "context":
        if selection is None:
            raise ValueError("a Context qualification requires its authored selection")
        if selection["mode"] == "whole_context":
            return {
                "selection": deep_thaw(selection),
                "context": semantic_owner_document(kind, document),
            }
        return {
            "selection": deep_thaw(selection),
            "context_qualifications": {
                key: deep_thaw(document[key])
                for key in _SCIENTIFIC_CONTEXT_QUALIFICATION_FIELDS
            },
            "treatments": {
                key: deep_thaw(document["treatments"][key])
                for key in selection["treatment_ids"]
                if key in document["treatments"]
            },
        }
    if selection is not None:
        raise ValueError("only Context sources have treatment selections")
    if kind == "evidence" and document.get("subtype") == "evidence_meaning":
        # Corrections carry the declared scientific qualifiers required for
        # recognition, not an Evidence dependency inventory or inferred summary.
        fields = (*_EVIDENCE_QUALIFICATION_SUBJECT_FIELDS["evidence_meaning"],
                  "objects", "hypotheses", "domain", "normalization", "quantifiers",
                  "parent_bet_outcome", "standing")
        return semantic_owner_document(kind, {
            field: (
                {key: document["subject"][key] for key in fields
                 if key in document["subject"]}
                if field == "subject" else document[field]
            )
            for field in _EVIDENCE_FIELDS if field in document
        })
    return _non_context_source_qualification_document(kind, document)


def _scientific_read_call(reference: Mapping[str, Any]) -> dict[str, Any]:
    return _call(
        "read", "Inspect the exact scientific owner and its qualifications.",
        ids=[retrieval_handle(reference)],
    )


def _local_scientific_read_failure(service: Any, error: Exception) -> str:
    # Reauthenticate the shared root in the SAME held cut. An authority/root
    # failure must propagate, never become a harmless missing scientific item.
    # Only after this succeeds is a selected payload/owner failure local.
    service._store.read_metadata()
    if isinstance(error, WorkspaceIntegrityError):
        # A valid current root is not sufficient proof that a failed historical
        # journal/path commitment was local. Only the selected immutable row's
        # own digest/shape diagnostics are classified here. Unknown authority,
        # path, origin, journal and Store failures remain operation-fatal.
        local_row_errors = {
            f"{kind.value} revision {suffix}"
            for kind in IdentityKind
            for suffix in (
                "identity, payload, or full-row digest mismatch",
                "is not one exact persisted owner row",
            )
        } | {
            "selected typed owner payload differs from its exact digest",
            "selected Evidence payload differs from its exact digest",
            "Evidence revision payload digest mismatch",
            "Evidence revision is not one exact persisted payload",
            "Evidence Blob ordinals are not contiguous",
        }
        if str(error) not in local_row_errors:
            raise error
    if isinstance(error, PermissionError):
        return "access_denied"
    if isinstance(error, StaleCommandError):
        if str(error).endswith(" is security-disposed"):
            return "access_denied"
        return "not_found"
    return "integrity_failure"


def scientific_context_in_snapshot(
    service: Any,
    mission_owner: Mapping[str, Any],
) -> dict[str, Any]:
    """Resolve independent scientific membership within the caller's held cut.

    Only the bound root, its explicit deeper selections and exposed source uses
    are read. There is no inventory, historical fallback, transitive expansion,
    refresh write, checkpoint prerequisite or scientific truth inference.
    """
    service._store.read_metadata()
    identity = mission_owner["document"].get("scientific_context_id")
    result: dict[str, Any] = {
        "state": "unbound", "binding": None, "root_reference": None,
        "purpose": None, "question": None, "treatments": [],
        "known_omissions": [], "restricted_uses": [], "restrictions": [],
        "independence_treatment": None,
        "source_changes": [], "unavailable": [],
    }
    if identity is None:
        return result
    if not isinstance(identity, str) or not identity.strip() or identity.startswith("context:"):
        raise WorkspaceIntegrityError("Mission scientific Context binding is not one bare identity")
    result["binding"] = {"context_id": identity}
    contexts: dict[str, tuple[Any, Any, str | None]] = {}
    sources: dict[tuple[str, str, int, str], tuple[Any, str | None]] = {}
    qualification_locations: dict[tuple[Any, ...], tuple[str, Any]] = {}

    def context_unavailable(context_id: str, treatment_id: str | None,
                            reason: str, reference: Any = None) -> None:
        item = {
            "kind": "context", "context_id": context_id,
            "treatment_id": treatment_id, "reason_code": reason,
            "read_call": None if reference is None else _scientific_read_call(reference),
        }
        if item not in result["unavailable"]:
            result["unavailable"].append(item)

    def source_unavailable(reference: Mapping[str, Any], reason: str,
                           *, readable_reference: Any = None) -> None:
        item = {
            "kind": "source", "reference": deep_thaw(reference),
            "reason_code": reason,
            "read_call": None if readable_reference is None
            else _scientific_read_call(readable_reference),
        }
        if item not in result["unavailable"]:
            result["unavailable"].append(item)

    def context_head(context_id: str) -> tuple[Any, Any, str | None]:
        if context_id in contexts:
            return contexts[context_id]
        reference = None
        try:
            stored = service._store.get_head(
                TypedWorkspaceId(IdentityKind.CONTEXT, context_id)
            )
            if stored is None:
                value = (None, None, "not_found")
            else:
                reference = {
                    "kind": "context", "identity": context_id,
                    "revision": stored.reference.revision,
                    "payload_sha256": stored.payload_digest,
                }
                document = stored.payload
                if (document.get("mission_id") != service._mission_id
                    or document.get("project_id") != service._store.project_id):
                    value = (None, None, "wrong_scope")
                elif document.get("schema_version") != 3:
                    value = (reference, None, "unsupported_version")
                else:
                    _validate_first_class_context_document(document)
                    value = (reference, document, None)
        except (StaleCommandError, WorkspaceIntegrityError, ValueError, PermissionError) as error:
            value = (reference, None, _local_scientific_read_failure(service, error))
        contexts[context_id] = value
        return value

    root_reference, root, failure = context_head(identity)
    result["root_reference"] = root_reference
    if failure is not None:
        result["state"] = "unavailable"
        context_unavailable(identity, None, failure, root_reference)
        return result
    result.update(state="available", purpose=root["purpose"], question=root["question"])
    result.update({key: deep_thaw(root[key]) for key in _SCIENTIFIC_CONTEXT_QUALIFICATION_FIELDS})
    selected = [(root_reference, key, value) for key, value in root["treatments"].items()]
    for exposure in root["exposed_treatments"]:
        child_id, treatment_id = exposure["context_id"], exposure["treatment_id"]
        child_reference, child, failure = context_head(child_id)
        if failure is not None:
            context_unavailable(child_id, treatment_id, failure, child_reference)
        elif treatment_id not in child["treatments"]:
            context_unavailable(child_id, treatment_id, "not_found", child_reference)
        else:
            selected.append((child_reference, treatment_id, child["treatments"][treatment_id]))

    uses: dict[tuple[Any, ...], dict[str, Any]] = {}
    for context_reference, treatment_id, treatment in selected:
        entry = {"context_reference": deep_thaw(context_reference), "treatment_id": treatment_id}
        if reference_key(context_reference) != reference_key(root_reference):
            # Direct membership is itself a source-use path: enclosing deeper
            # restrictions must travel even when no source head has advanced.
            # Do not substitute this Context's sibling treatments or exposures.
            _, selected_context, _ = contexts[context_reference["identity"]]
            entry["context_qualifications"] = {
                field: deep_thaw(selected_context[field])
                for field in _SCIENTIFIC_CONTEXT_QUALIFICATION_FIELDS
            }
        entry["content"] = deep_thaw(treatment)
        result["treatments"].append(entry)
        for source in treatment["sources"]:
            # Background-only history remains exact authored content but does
            # not require a current-head correction exposure.
            if not {"recognition", "reliance"}.intersection(source["roles"]):
                continue
            use_key = (*reference_key(source["reference"]), canonical_json_bytes(source["selection"]))
            use = uses.setdefault(use_key, {
                "source": source, "affected": [],
            })
            affected = {"context_reference": deep_thaw(context_reference),
                        "treatment_id": treatment_id, "roles": deep_thaw(source["roles"])}
            if affected not in use["affected"]:
                use["affected"].append(affected)

    def exact_source(reference: Mapping[str, Any]) -> tuple[Any, str | None]:
        key = reference_key(reference)
        if key in sources:
            return sources[key]
        try:
            owner = service._store._read_current_bound_owner_revision(**reference)
            if (owner["mission_id"] != service._mission_id
                or owner["document"].get("project_id", service._store.project_id)
                != service._store.project_id):
                value = (None, "wrong_scope")
            elif reference_key(owner["reference"]) != key:
                value = (None, "integrity_failure")
            else:
                if reference["kind"] == "context":
                    _validate_first_class_context_document(owner["document"])
                value = (owner, None)
        except (StaleCommandError, WorkspaceIntegrityError, ValueError, PermissionError) as error:
            value = (None, _local_scientific_read_failure(service, error))
        sources[key] = value
        return value

    for use in uses.values():
        source = use["source"]
        reference, selection = source["reference"], source["selection"]
        pinned, failure = exact_source(reference)
        change = {
            "cited_reference": deep_thaw(reference), "selection": deep_thaw(selection),
            "current_reference": None, "state": "current_unavailable",
            "affected": use["affected"], "qualification": None,
            "qualification_ref": None, "read_call": None,
        }
        if failure is not None:
            source_unavailable(reference, failure)
            result["source_changes"].append(change)
            continue
        if reference["kind"] == "context" and selection["mode"] == "treatments":
            if pinned["document"].get("schema_version") != 3:
                source_unavailable(reference, "unsupported_version", readable_reference=reference)
                result["source_changes"].append(change)
                continue
            missing = [key for key in selection["treatment_ids"]
                       if key not in pinned["document"]["treatments"]]
            if missing:
                # The newer treatment cannot repair an absent exact cited use.
                source_unavailable(reference, "not_found", readable_reference=reference)
                result["source_changes"].append(change)
                continue
        current_reference = pinned["current_reference"]
        if current_reference is None:
            change["state"] = "no_current_head"
            change["read_call"] = _scientific_read_call(reference)
            result["source_changes"].append(change)
            continue
        change["current_reference"] = deep_thaw(current_reference)
        current, failure = exact_source(current_reference)
        if failure is not None:
            source_unavailable(current_reference, failure, readable_reference=reference)
            result["source_changes"].append(change)
            continue
        change["read_call"] = _scientific_read_call(current_reference)
        if reference["kind"] == "context" and selection["mode"] == "treatments":
            if current["document"].get("schema_version") != 3:
                source_unavailable(current_reference, "unsupported_version",
                                   readable_reference=current_reference)
                result["source_changes"].append(change)
                continue
            for treatment_id in selection["treatment_ids"]:
                if treatment_id not in current["document"]["treatments"]:
                    context_unavailable(current_reference["identity"], treatment_id,
                                        "not_found", current_reference)
            if not any(key in current["document"]["treatments"] for key in selection["treatment_ids"]):
                result["source_changes"].append(change)
                continue
        if reference_key(reference) == reference_key(current_reference):
            continue
        try:
            qualification = source_qualification_document(
                str(reference["kind"]), current["document"], selection
            )
        except WorkspaceIntegrityError as error:
            if str(error) != "executive_orientation_evidence_subtype_invalid":
                raise
            source_unavailable(current_reference, "unsupported_version",
                               readable_reference=current_reference)
            result["source_changes"].append(change)
            continue
        change["state"] = "advanced_same_identity"
        qualification_key = (
            *reference_key(current_reference), canonical_json_bytes(selection)
        )
        existing = qualification_locations.get(qualification_key)
        if existing is not None and canonical_json_bytes(existing[1]) == canonical_json_bytes(qualification):
            change["qualification_ref"] = {"orientation_path": existing[0]}
        else:
            change["qualification"] = qualification
            qualification_locations[qualification_key] = (
                f"/scientific_context/source_changes/{len(result['source_changes'])}/qualification",
                qualification,
            )
        result["source_changes"].append(change)
    if result["unavailable"]:
        result["state"] = "partial"
    service._store.read_metadata()
    return result


def executive_orientation_in_snapshot(service: Any) -> dict[str, Any]:
    canonical_snapshot = canonical_snapshot_in_snapshot(service)
    # Authenticate the current root6 boundary before operation-local reads.
    service._store.read_metadata()
    checkpoint = service._store.read_current_mission_checkpoint_summary(
        service._mission_id
    )
    mission_ref, _ = service._resolve_owner_selector(
        {"id": f"mission:{service._mission_id}"}
    )
    mission = service._read_recovery_owner_revision(
        kind="mission", identity=service._mission_id, revision=mission_ref["revision"]
    )
    strategy = current_strategy_in_snapshot(service, mission)
    strategy_ref = strategy.to_reference()
    proof = proof_attention_in_snapshot(service, canonical_snapshot=canonical_snapshot)
    current_strategy = {
        "handle": retrieval_handle(strategy_ref),
        **semantic_owner_document("strategy", strategy.record.document),
        "formal_requests": _formal_requests(strategy),
    }
    counts: dict[str, dict[str, int]] = {}
    document = checkpoint
    mission_strategy_roots_changed = False
    if document is not None:
        for kind, before, current in (
            ("mission", document["mission_root"], mission_ref),
            ("strategy", document["strategy_root"], strategy_ref),
        ):
            if reference_key(before) == reference_key(current):
                continue
            mission_strategy_roots_changed = True
            bucket = counts.setdefault(
                kind, {"new": 0, "advanced": 0, "retired": 0}
            )
            if (before["kind"], before["identity"]) == (
                current["kind"],
                current["identity"],
            ):
                bucket["advanced"] += 1
            else:
                bucket["retired"] += 1
                bucket["new"] += 1
    checkpoint_summary = (
        None
        if document is None
        else {
            "checkpoint_id": document["checkpoint_id"],
            "mission_root_handle": retrieval_handle(document["mission_root"]),
            "strategy_root_handle": retrieval_handle(document["strategy_root"]),
            "predecessor_checkpoint_id": None
            if document["predecessor_checkpoint"] is None
            else document["predecessor_checkpoint"]["checkpoint_id"],
        }
    )
    unresolved_count = (
        0 if document is None else int(document["unresolved_pointer_count"])
    )
    continuity = {
        "checkpoint": checkpoint_summary,
        "mission_relation_to_checkpoint": "no_checkpoint"
        if document is None
        else _relation(document["mission_root"], mission_ref),
        "strategy_relation_to_checkpoint": "no_checkpoint"
        if document is None
        else _relation(document["strategy_root"], strategy_ref),
        "mission_strategy_changes_since_checkpoint": {
            "state": "no_checkpoint"
            if document is None
            else ("present" if mission_strategy_roots_changed else "none"),
            "counts_by_kind": counts,
            "retrieve_call": _call(
                "changes_since_checkpoint",
                "Inspect factual owner changes after a Mission or Strategy root change.",
                page_size=25,
            )
            if mission_strategy_roots_changed
            else None,
        },
        "checkpoint_attention": {
            "unresolved_pointer_count": unresolved_count,
            "retrieve_call": _call(
                "checkpoint",
                "Inspect retained unresolved checkpoint pointers.",
                checkpoint_id=document["checkpoint_id"],
                section="unresolved_pointers",
                page_size=25,
            )
            if unresolved_count
            else None,
        },
    }
    formal = formal_attention_in_snapshot(service, mission_ref, strategy)
    admitted = proof["admitted_result"]
    # A retained unsolved closeout is historical Strategy meaning, not terminal
    # canonical authority.  Only the exact admitted-result projection narrows
    # the Executive to the closeout-only request and retrieval surface.
    closeout = admitted is not None
    if closeout:
        continuity["mission_strategy_changes_since_checkpoint"]["retrieve_call"] = None
        continuity["checkpoint_attention"]["retrieve_call"] = None
    recommended = []
    if not closeout:
        if mission_strategy_roots_changed:
            recommended.append(
                continuity["mission_strategy_changes_since_checkpoint"]["retrieve_call"]
            )
        if unresolved_count:
            recommended.append(continuity["checkpoint_attention"]["retrieve_call"])
        if proof["open_candidate_a1"]:
            recommended.append(
                _call(
                    "proof_attention",
                    "Inspect exact OPEN Candidate A1 attention.",
                    page_size=25,
                )
            )
    if verify_snapshot_current(canonical_snapshot) is not None:
        raise WorkspaceIntegrityError(
            "executive_orientation_canonical_binding_mismatch"
        )
    result = {
        "schema_version": ORIENTATION_SCHEMA,
        "target": {
            "target": "riemann_hypothesis",
            "statement": "Every nontrivial zero of the Riemann zeta function has real part one half.",
            "canonical_status": _canonical_status(canonical_snapshot, admitted),
        },
        "mission": {
            "handle": retrieval_handle(mission_ref),
            **semantic_owner_document("mission", mission["document"]),
        },
        "current_strategy": current_strategy,
        "scientific_context": scientific_context_in_snapshot(service, mission),
        "continuity": continuity,
        "proof_attention": proof,
        "formal_attention": formal,
        "retrieval": {
            "usage_call": {
                "operation": "usage",
                "input": {"for_operation": "checkpoint" if closeout else "retrieve"},
            },
            "available_modes": [] if closeout else list(ROOT_RETRIEVAL_MODES),
            "recommended_calls": recommended,
        },
    }
    from .mission_operation_contract import validate_executive_orientation

    validate_executive_orientation(result)
    return result
