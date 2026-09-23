#!/usr/bin/env python3
"""Execute the two operator-owned effects after an exact RH Admission Decision.

Admission Case, Review, and Decision records are proof-neutral.  This tool is
the operator-owned mechanical boundary that either writes the one Decision-derived
canonical Research State delta in a clean worktree or rebinds an inactive
retained Workspace to an already-selected release containing that exact delta.
It never decides mathematics, changes Strategy, admits an epoch, or publishes.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from pathlib import Path
from typing import Any, Mapping, Sequence


REPO_ROOT = Path(__file__).resolve().parents[1]
PACKAGE_ROOT = (
    REPO_ROOT / "packages" / "research-core"
)
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))
if str(PACKAGE_ROOT) not in sys.path:
    sys.path.insert(0, str(PACKAGE_ROOT))

from research_core.canonical_admission import (  # noqa: E402
    CanonicalAdmissionError,
    CanonicalAdmissionWriteResult,
    preview_admitted_result,
    write_admitted_result,
)
from research_core.canonical_snapshot import (  # noqa: E402
    CanonicalStateSnapshot,
    load_canonical_snapshot,
    verify_snapshot_integrity,
)
from research_core.complete_claim_admission import (  # noqa: E402
    ADMISSION_REVIEW_NO_MATERIAL_OBJECTION,
    ADMISSION_DECISION_AUTHORIZE,
    AdmissionCaseRecord,
    AdmissionDecisionRecord,
    AdmissionEvidenceRef,
    AdmissionReviewRecord,
    admission_case_from_evidence,
    admission_decision_from_evidence,
    admission_review_from_evidence,
)
from research_core.evidence_store import EvidenceItemRevision  # noqa: E402
from research_core.json_support import (  # noqa: E402
    canonical_json_bytes,
    json_compatible,
    loads_strict_json_object,
)
from research_core.workspace_paths import (  # noqa: E402
    WorkspacePathError,
    WorkspacePaths,
)
from research_core.workspace_store import (  # noqa: E402
    AdmittedResultRebindResult,
    WorkspaceStore,
    WorkspaceStoreError,
)


TOOL_ID = "tool.mathematical_research.rh_admission"
ENTRYPOINT = "python scripts/rh_admission.py"
PROJECT_ID = "project.riemann_hypothesis"
RELEASE_RECORD_RELATIVE_PATH = Path(".mathematical-research-release.json")
RELEASE_RECORD_SCHEMA = "wc.rh_mission_host_release.v1"
REBIND_REQUEST_SCHEMA = "mathematical_research.admitted_result_rebind_request.v1"
FETCH_SCHEMA = "wc.rh_mission_host_fetch.v1"
FETCH_BINDING_SCHEMA = "wc.rh_mission_host_binding.v1"
RECONSTRUCTION_SCHEMA = "mathematical_research.direct_mission_reconstruction.v4"

_SHA256_RE = re.compile(r"[0-9a-f]{64}")
_FULL_GIT_SHA_RE = re.compile(r"[0-9a-f]{40}")
_IDENTITY_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]*")
_VERBS = frozenset({"usage", "write-canonical-result", "rebind-canonical-result"})
_REBIND_REQUEST_KEYS = frozenset(
    {
        "schema_version",
        "mission_id",
        "project_id",
        "workspace_root",
        "selected_release_sha",
        "admission_decision_ref",
        "source_canonical_authority_digest",
        "require_latest_durable_checkpoint_predecessor",
        "require_no_admitted_successor",
    }
)


class AdmissionToolError(ValueError):
    """Reject an ambiguous or stale operator request before any mutation."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class _Parser(argparse.ArgumentParser):
    def error(self, message: str) -> None:
        raise AdmissionToolError("invalid_invocation", message)


def _usage_contract() -> dict[str, Any]:
    return {
        "schema_version": "tool_usage.v1",
        "tool_id": TOOL_ID,
        "status": "ok",
        "purpose": (
            "Mechanically apply one exact persisted authorize Admission Decision "
            "to canonical RH truth, then rebind one fully inactive retained "
            "Workspace to an exact selected release containing that result."
        ),
        "entrypoint": ENTRYPOINT,
        "safe_first_calls": [
            {"label": "usage", "cmd": f"{ENTRYPOINT} usage --format json"},
        ],
        "verbs": [
            {
                "name": "usage",
                "side_effects": "none",
                "summary": "Return this usage contract.",
            },
            {
                "name": "write-canonical-result",
                "side_effects": (
                    "atomically writes only the canonical RH Research State file"
                ),
                "summary": (
                    "Apply the exact Decision-derived proved or disproved delta; "
                    "does not commit or push Git."
                ),
            },
            {
                "name": "rebind-canonical-result",
                "side_effects": (
                    "atomically rebinds one inactive retained Mission Workspace"
                ),
                "summary": (
                    "Verify the selected release and exact persisted Decision, "
                    "then invoke the Store-owned idempotent authority rebind."
                ),
            },
        ],
        "selectors": [
            {
                "name": "--root",
                "required": "write-canonical-result",
                "summary": "Clean repository worktree containing canonical Research State.",
            },
            {
                "name": "--admission-readback",
                "required": "write-canonical-result",
                "summary": (
                    "Exact ignored inactive-fetch latest.json containing the persisted "
                    "Admission Case, Review, Decision, and Candidate readback."
                ),
            },
            {
                "name": "--workspace-root",
                "required": "rebind-canonical-result",
                "summary": "Exact retained Mathematical Research Workspace root.",
            },
            {
                "name": "--project-id",
                "required": "both effectful verbs",
                "summary": f"Must be {PROJECT_ID}.",
            },
            {
                "name": "--mission-id",
                "required": "both effectful verbs",
                "summary": "Exact Mission owning the persisted Admission Decision.",
            },
            {
                "name": "--admission-decision-id",
                "required": "write-canonical-result",
                "summary": "Exact persisted authorize Admission Decision Evidence id.",
            },
            {
                "name": "--admission-decision-digest",
                "required": "write-canonical-result",
                "summary": "Exact persisted Admission Decision payload SHA-256.",
            },
            {
                "name": "--source-commit",
                "required": "write-canonical-result",
                "summary": "Full Git SHA whose canonical prestate bytes must match.",
            },
            {
                "name": "--expected-prestate-sha256",
                "required": "write-canonical-result",
                "summary": "Exact canonical Research State prestate byte digest.",
            },
            {
                "name": "--request",
                "required": "rebind-canonical-result",
                "summary": "Host-created private exact rebind request JSON.",
            },
            {
                "name": "--dry-run",
                "required": False,
                "summary": (
                    "Execute the selected verb's exact validation and result "
                    "calculation without changing canonical or Workspace state."
                ),
            },
        ],
        "output_formats": ["text", "json"],
        "side_effects": {
            "writes_repo": True,
            "writes_paths": [
                "projects/riemann_hypothesis/research_state.json",
                "<workspace-root>/workspace.sqlite3",
            ],
            "modifies_remote": False,
            "network_access": "none",
            "danger_level": "high",
            "supports_dry_run": True,
            "dry_run_boundary": (
                "Both effectful verbs validate the same exact prestate and compute "
                "their expected result; canonical rendering is pure and Workspace "
                "rebind runs in the owning transaction then rolls it back."
            ),
        },
        "examples": [
            {
                "label": "Preview canonical result",
                "cmd": (
                    f"{ENTRYPOINT} write-canonical-result --root <clean-worktree> "
                    "--admission-readback <inactive-fetch-latest.json> "
                    f"--project-id {PROJECT_ID} --mission-id <mission-id> "
                    "--admission-decision-id <decision-id> "
                    "--admission-decision-digest <64hex> --source-commit <40hex> "
                    "--expected-prestate-sha256 <64hex> --dry-run --format json"
                ),
            },
            {
                "label": "Preview retained Workspace rebind",
                "cmd": (
                    f"{ENTRYPOINT} rebind-canonical-result "
                    "--request <private-request.json> "
                    "--workspace-root <retained-workspace> "
                    f"--project-id {PROJECT_ID} --mission-id <mission-id> "
                    "--dry-run --format json"
                ),
            },
        ],
        "authority_boundary": {
            "decides_mathematics": False,
            "changes_candidate_or_a1": False,
            "changes_mission_or_strategy": False,
            "admits_successor": False,
            "public_effect": "none",
        },
        "errors": [
            {
                "code": "invalid_invocation",
                "meaning": "The command or one semantic selector is malformed.",
            },
            {
                "code": "admission_decision_invalid",
                "meaning": "The exact persisted Decision is absent, stale, or not authorizing.",
            },
            {
                "code": "canonical_result_write_failed",
                "meaning": "The exact canonical prestate or authorized delta was rejected.",
            },
            {
                "code": "admitted_result_rebind_failed",
                "meaning": "The selected release or inactive Workspace rebind was rejected.",
            },
        ],
    }


def _result(
    *,
    status: str,
    verb: str,
    summary: str,
    data: Mapping[str, Any],
    errors: list[dict[str, str]] | None = None,
) -> dict[str, Any]:
    return {
        "schema_version": "tool_result.v1",
        "tool_id": TOOL_ID,
        "status": status,
        "verb": verb,
        "summary": summary,
        "data": json_compatible(data),
        "warnings": [],
        "errors": errors or [],
    }


def _emit(payload: Mapping[str, Any], output_format: str) -> None:
    if output_format == "json":
        print(json.dumps(payload, indent=2, sort_keys=True, allow_nan=False))
        return
    if payload.get("schema_version") == "tool_usage.v1":
        print(f"{TOOL_ID}: {payload['purpose']}")
        print("verbs: usage, write-canonical-result, rebind-canonical-result")
        print("preview: add --dry-run to either effectful verb")
        print(f"safe first call: {ENTRYPOINT} usage --format json")
        return
    print(f"{payload['status']}: {payload['summary']}")
    for key, value in payload.get("data", {}).items():
        print(f"{key}: {value}")
    for error in payload.get("errors", []):
        print(f"error[{error['code']}]: {error['message']}")


def _extract_global_options(
    argv: Sequence[str],
) -> tuple[list[str], Path, str, bool]:
    cleaned: list[str] = []
    repo_root = REPO_ROOT
    output_format = "text"
    dry_run = False
    seen: set[str] = set()
    index = 0
    while index < len(argv):
        item = argv[index]
        if item in {"--root", "--format"}:
            if item in seen:
                raise AdmissionToolError(
                    "invalid_invocation", f"{item} may be supplied only once"
                )
            if index + 1 >= len(argv):
                raise AdmissionToolError(
                    "invalid_invocation", f"{item} requires one value"
                )
            seen.add(item)
            value = argv[index + 1]
            if item == "--root":
                repo_root = Path(value)
            else:
                output_format = value
            index += 2
            continue
        if item == "--json":
            if "--format" in seen or "--json" in seen:
                raise AdmissionToolError(
                    "invalid_invocation", "choose exactly one output-format selector"
                )
            seen.add("--json")
            output_format = "json"
            index += 1
            continue
        if item == "--dry-run":
            if item in seen:
                raise AdmissionToolError(
                    "invalid_invocation", "--dry-run may be supplied only once"
                )
            seen.add(item)
            dry_run = True
            index += 1
            continue
        cleaned.append(item)
        index += 1
    if output_format not in {"text", "json"}:
        raise AdmissionToolError("invalid_invocation", "--format must be text or json")
    return cleaned, repo_root.resolve(), output_format, dry_run


def _parser() -> argparse.ArgumentParser:
    parser = _Parser(
        prog=ENTRYPOINT,
        description="Execute the private mechanical effects of exact RH Admission.",
    )
    subparsers = parser.add_subparsers(dest="verb", required=True)
    subparsers.add_parser("usage")

    write = subparsers.add_parser("write-canonical-result")
    write.add_argument("--admission-readback", required=True)
    write.add_argument("--project-id", required=True)
    write.add_argument("--mission-id", required=True)
    write.add_argument("--admission-decision-id", required=True)
    write.add_argument("--admission-decision-digest", required=True)
    write.add_argument("--source-commit", required=True)
    write.add_argument("--expected-prestate-sha256", required=True)

    rebind = subparsers.add_parser("rebind-canonical-result")
    rebind.add_argument("--request", required=True)
    rebind.add_argument("--workspace-root", required=True)
    rebind.add_argument("--project-id", required=True)
    rebind.add_argument("--mission-id", required=True)
    return parser


def _require_identity(value: object, location: str) -> str:
    if type(value) is not str or _IDENTITY_RE.fullmatch(value) is None:
        raise AdmissionToolError(
            "invalid_invocation", f"{location} must be one exact identity"
        )
    return value


def _require_digest(value: object, location: str) -> str:
    if type(value) is not str or _SHA256_RE.fullmatch(value) is None:
        raise AdmissionToolError(
            "invalid_invocation", f"{location} must be lowercase SHA-256"
        )
    return value


def _require_commit(value: object, location: str) -> str:
    if type(value) is not str or _FULL_GIT_SHA_RE.fullmatch(value) is None:
        raise AdmissionToolError(
            "invalid_invocation", f"{location} must be one full lowercase Git SHA"
        )
    return value


def _require_project(value: object) -> str:
    if value != PROJECT_ID:
        raise AdmissionToolError(
            "invalid_invocation", f"project_id must be exactly {PROJECT_ID}"
        )
    return PROJECT_ID


def _decision_ref(decision_id: object, digest: object) -> AdmissionEvidenceRef:
    return AdmissionEvidenceRef(
        evidence_id=_require_identity(decision_id, "admission_decision_id"),
        revision=1,
        payload_sha256=_require_digest(digest, "admission_decision_digest"),
    )


def _open_store(workspace_root: str, project_id: str) -> WorkspaceStore:
    return WorkspaceStore.open(
        WorkspacePaths.from_root(workspace_root),
        expected_project_id=project_id,
    )


def _read_authorizing_decision(
    store: WorkspaceStore,
    *,
    decision_ref: AdmissionEvidenceRef,
    mission_id: str,
):
    decision = store.read_complete_claim_admission_decision(decision_ref)
    if decision.candidate_ref.mission_id != mission_id:
        raise AdmissionToolError(
            "admission_decision_invalid",
            "the persisted Admission Decision belongs to another Mission",
        )
    if decision.disposition != ADMISSION_DECISION_AUTHORIZE:
        raise AdmissionToolError(
            "admission_decision_invalid",
            "only an exact authorize Admission Decision has a canonical route",
        )
    return decision


_EVIDENCE_DOCUMENT_KEYS = frozenset(
    {
        "evidence_id",
        "revision",
        "subtype",
        "subject",
        "exact_scope",
        "rigor",
        "limitations",
        "non_inferences",
        "security_classification",
        "retention",
        "blob_roles",
        "availability_state",
        "canonical_effect",
    }
)


def _readback_evidence(
    selected: object,
    reference: AdmissionEvidenceRef,
) -> EvidenceItemRevision:
    if not isinstance(selected, list):
        raise AdmissionToolError(
            "admission_readback_invalid",
            "inactive fetch has no exact selected owner readback",
        )
    matches: list[Mapping[str, Any]] = []
    for item in selected:
        if not isinstance(item, Mapping):
            continue
        current = item.get("current_reference")
        if (
            item.get("readback_kind") == "current_owner_document"
            and isinstance(current, Mapping)
            and current.get("kind") == "evidence"
            and current.get("identity") == reference.evidence_id
            and current.get("revision") == reference.revision
            and current.get("payload_sha256") == reference.payload_sha256
        ):
            matches.append(item)
    if len(matches) != 1:
        raise AdmissionToolError(
            "admission_readback_invalid",
            "inactive fetch does not contain exactly one requested Admission Evidence revision",
        )
    document = matches[0].get("document")
    if not isinstance(document, Mapping) or set(document) != _EVIDENCE_DOCUMENT_KEYS:
        raise AdmissionToolError(
            "admission_readback_invalid",
            "Admission Evidence readback has the wrong closed shape",
        )
    blob_roles = document.get("blob_roles")
    if not isinstance(blob_roles, list) or any(
        not isinstance(item, list)
        or len(item) != 2
        or any(type(value) is not str for value in item)
        for item in blob_roles
    ):
        raise AdmissionToolError(
            "admission_readback_invalid",
            "Admission Evidence readback has invalid blob roles",
        )
    try:
        evidence = EvidenceItemRevision(
            evidence_id=document["evidence_id"],
            revision=document["revision"],
            subtype=document["subtype"],
            subject=document["subject"],
            exact_scope=document["exact_scope"],
            rigor=document["rigor"],
            limitations=tuple(document["limitations"]),
            non_inferences=tuple(document["non_inferences"]),
            security_classification=document["security_classification"],
            retention=document["retention"],
            blob_roles=tuple(tuple(item) for item in blob_roles),
            availability_state=document["availability_state"],
            canonical_effect=document["canonical_effect"],
        )
    except (TypeError, ValueError) as exc:
        raise AdmissionToolError(
            "admission_readback_invalid",
            "Admission Evidence readback failed exact reconstruction",
        ) from exc
    if (
        evidence.evidence_id != reference.evidence_id
        or evidence.revision != reference.revision
        or hashlib.sha256(canonical_json_bytes(evidence.to_payload())).hexdigest()
        != reference.payload_sha256
    ):
        raise AdmissionToolError(
            "admission_readback_invalid",
            "Admission Evidence bytes differ from their exact selected reference",
        )
    return evidence


def _require_frozen_candidate_readback(
    selected: object,
    *,
    case: AdmissionCaseRecord,
) -> None:
    if not isinstance(selected, list):
        raise AdmissionToolError(
            "admission_readback_invalid", "inactive fetch has no Candidate readback"
        )
    candidate = case.candidate_ref
    handle = f"candidate:{candidate.candidate_id}@{candidate.revision}"
    matches = [
        item
        for item in selected
        if isinstance(item, Mapping) and item.get("retrieval_handle") == handle
    ]
    if len(matches) != 1:
        raise AdmissionToolError(
            "admission_readback_invalid",
            "inactive fetch does not contain exactly one frozen Candidate A1 revision",
        )
    item = matches[0]
    reference = item.get("current_reference")
    document = item.get("document")
    a1 = item.get("candidate_a1")
    if (
        item.get("readback_kind")
        not in {"current_owner_document", "open_candidate_a1_document"}
        or not isinstance(reference, Mapping)
        or reference
        != {
            "kind": "candidate",
            "identity": candidate.candidate_id,
            "revision": candidate.revision,
            "payload_sha256": candidate.digest_sha256,
        }
        or not isinstance(document, Mapping)
        or hashlib.sha256(canonical_json_bytes(document)).hexdigest()
        != candidate.digest_sha256
        or not isinstance(a1, Mapping)
        or a1.get("candidate_ref") != reference
        or a1.get("hold_lifecycle") != "open"
        or a1.get("canonical_effect") != "none"
        or a1.get("mathematical_effect") != "none"
    ):
        raise AdmissionToolError(
            "admission_readback_invalid",
            "frozen Candidate readback differs from its OPEN proof-neutral A1",
        )
    triage = a1.get("triage")
    expected_triage = {
        "kind": "evidence",
        "identity": case.triage_ref.evidence_id,
        "revision": case.triage_ref.revision,
        "payload_sha256": case.triage_ref.payload_sha256,
    }
    claim = document.get("complete_target_claim")
    if (
        not isinstance(triage, Mapping)
        or triage.get("disposition") != "admission_ready"
        or triage.get("evidence_ref") != expected_triage
        or triage.get("retrieval_handle")
        != f"evidence:{case.triage_ref.evidence_id}@{case.triage_ref.revision}"
        or document.get("exact_statement") != case.exact_claim
        or not isinstance(claim, Mapping)
        or claim.get("target") != "riemann_hypothesis"
        or claim.get("disposition") != case.candidate_disposition
    ):
        raise AdmissionToolError(
            "admission_readback_invalid",
            "frozen Candidate readback does not carry the exact admission_ready complete claim",
        )


def _validate_authorizing_chain(
    *,
    case: AdmissionCaseRecord,
    review: AdmissionReviewRecord,
    decision: AdmissionDecisionRecord,
    mission_id: str,
) -> AdmissionDecisionRecord:
    case_ref = AdmissionEvidenceRef(
        case.evidence.evidence_id, case.evidence.revision, case.payload_digest
    )
    review_ref = AdmissionEvidenceRef(
        review.evidence.evidence_id, review.evidence.revision, review.payload_digest
    )
    candidate_basis = {
        "kind": "candidate",
        "identity": case.candidate_ref.candidate_id,
        "revision": case.candidate_ref.revision,
        "payload_sha256": case.candidate_ref.digest_sha256,
    }
    authorized_basis = {
        canonical_json_bytes(item.to_payload()) for item in case.source_closure
    } | {canonical_json_bytes(candidate_basis)}
    if (
        case.project_id != PROJECT_ID
        or case.candidate_ref.mission_id != mission_id
        or case.candidate_author_thread_id == case.triage_reviewer_thread_id
        or review.candidate_ref != case.candidate_ref
        or review.case_ref != case_ref
        or review.disposition != ADMISSION_REVIEW_NO_MATERIAL_OBJECTION
        or review.objections
        or any(
            canonical_json_bytes(item.to_payload()) not in authorized_basis
            for item in review.cited_basis
        )
        or decision.candidate_ref != case.candidate_ref
        or decision.case_ref != case_ref
        or decision.review_ref != review_ref
        or decision.disposition != ADMISSION_DECISION_AUTHORIZE
        or review.reviewer.thread_id
        in {
            case.candidate_author_thread_id,
            case.triage_reviewer_thread_id,
        }
        or decision.admitter.thread_id
        in {
            case.candidate_author_thread_id,
            case.triage_reviewer_thread_id,
            review.reviewer.thread_id,
        }
        or not isinstance(decision.canonical_result_binding, Mapping)
        or decision.canonical_result_binding.get("exact_claim") != case.exact_claim
        or decision.canonical_result_binding.get("candidate_disposition")
        != case.candidate_disposition
    ):
        raise AdmissionToolError(
            "admission_decision_invalid",
            "inactive fetch does not preserve one exact role-disjoint authorizing Admission chain",
        )
    return decision


def _read_authorizing_decision_from_readback(
    path_value: str,
    *,
    decision_ref: AdmissionEvidenceRef,
    project_id: str,
    mission_id: str,
    source_commit: str,
) -> AdmissionDecisionRecord:
    path = Path(path_value)
    if path.is_symlink() or not path.is_file():
        raise AdmissionToolError(
            "admission_readback_invalid",
            "admission_readback must be one ordinary existing inactive fetch file",
        )
    try:
        payload = loads_strict_json_object(path.read_bytes())
    except (OSError, UnicodeError, TypeError, ValueError) as exc:
        raise AdmissionToolError(
            "admission_readback_invalid",
            "admission_readback must contain one strict fetch object",
        ) from exc
    binding = payload.get("binding")
    systemd = payload.get("systemd")
    offline = payload.get("offline_transition")
    host_state = payload.get("host_state")
    reconstruction = payload.get("mission_reconstruction")
    if (
        payload.get("schema") != FETCH_SCHEMA
        or payload.get("mission_id") != mission_id
        or payload.get("release_sha") != source_commit
        or not isinstance(binding, Mapping)
        or binding.get("schema") != FETCH_BINDING_SCHEMA
        or binding.get("mission_id") != mission_id
        or binding.get("release_sha") != source_commit
        or not isinstance(systemd, Mapping)
        or systemd.get("ActiveState") != "inactive"
        or systemd.get("SubState") != "dead"
        or systemd.get("MainPID") != 0
        or systemd.get("UnitFileState") != "disabled"
        or not isinstance(offline, Mapping)
        or offline.get("service_quiesced") is not True
        or offline.get("runtime_lock_present") is not False
        or offline.get("pending") is not False
        or offline.get("binding_matches_selected") is not True
        or offline.get("inactive_fetch_eligible") is not True
        or not isinstance(host_state, Mapping)
        or host_state.get("missionId") != mission_id
        or host_state.get("activeGoal") is not None
        or not isinstance(reconstruction, Mapping)
        or reconstruction.get("schema_version") != RECONSTRUCTION_SCHEMA
        or reconstruction.get("project_id") != project_id
        or reconstruction.get("mission_id") != mission_id
        or type(reconstruction.get("observed_project_commit")) is not int
    ):
        raise AdmissionToolError(
            "admission_readback_invalid",
            "Admission write requires one coherent inactive selected-release fetch",
        )
    latest = reconstruction.get("latest_executive_epoch")
    opening = reconstruction.get("recovery_opening")
    if (
        not isinstance(latest, Mapping)
        or latest.get("state") != "checkpointed"
        or not isinstance(opening, Mapping)
        or opening.get("admitted_result") is not None
        or not isinstance(opening.get("cut"), Mapping)
    ):
        raise AdmissionToolError(
            "admission_readback_invalid",
            "Admission write requires the latest durable checkpoint before canonical result",
        )
    selected = reconstruction.get("selected_readback")
    try:
        decision = admission_decision_from_evidence(
            _readback_evidence(selected, decision_ref)
        )
        case = admission_case_from_evidence(
            _readback_evidence(selected, decision.case_ref)
        )
        review = admission_review_from_evidence(
            _readback_evidence(selected, decision.review_ref)
        )
    except AdmissionToolError:
        raise
    except (TypeError, ValueError) as exc:
        raise AdmissionToolError(
            "admission_readback_invalid",
            "fetched Admission chain failed exact reconstruction",
        ) from exc
    _readback_evidence(selected, case.triage_ref)
    _require_frozen_candidate_readback(selected, case=case)
    return _validate_authorizing_chain(
        case=case,
        review=review,
        decision=decision,
        mission_id=mission_id,
    )


def _write_canonical_result(
    arguments: argparse.Namespace, repo_root: Path, *, dry_run: bool
) -> Mapping[str, Any]:
    project_id = _require_project(arguments.project_id)
    mission_id = _require_identity(arguments.mission_id, "mission_id")
    source_commit = _require_commit(arguments.source_commit, "source_commit")
    expected_prestate = _require_digest(
        arguments.expected_prestate_sha256, "expected_prestate_sha256"
    )
    reference = _decision_ref(
        arguments.admission_decision_id,
        arguments.admission_decision_digest,
    )
    decision = _read_authorizing_decision_from_readback(
        arguments.admission_readback,
        decision_ref=reference,
        project_id=project_id,
        mission_id=mission_id,
        source_commit=source_commit,
    )
    apply = preview_admitted_result if dry_run else write_admitted_result
    result = apply(
        repo_root,
        source_commit=source_commit,
        expected_prestate_sha256=expected_prestate,
        decision=decision,
    )
    if type(result) is not CanonicalAdmissionWriteResult:
        raise AdmissionToolError(
            "canonical_result_write_failed",
            "the canonical writer returned an unsupported result",
        )
    return result.to_payload()


def _load_rebind_request(path_value: str) -> Mapping[str, Any]:
    path = Path(path_value)
    if path.is_symlink() or not path.is_file():
        raise AdmissionToolError(
            "invalid_invocation", "request must be one ordinary existing file"
        )
    try:
        payload = loads_strict_json_object(path.read_bytes())
    except (OSError, UnicodeError, TypeError, ValueError) as exc:
        raise AdmissionToolError(
            "invalid_invocation", "request must be one strict JSON object"
        ) from exc
    if set(payload) != _REBIND_REQUEST_KEYS:
        raise AdmissionToolError(
            "invalid_invocation", "rebind request must have the exact closed shape"
        )
    if payload.get("schema_version") != REBIND_REQUEST_SCHEMA:
        raise AdmissionToolError(
            "invalid_invocation", "rebind request schema is unsupported"
        )
    if payload.get("require_latest_durable_checkpoint_predecessor") is not True:
        raise AdmissionToolError(
            "invalid_invocation",
            "rebind requires the latest durable checkpoint predecessor",
        )
    if payload.get("require_no_admitted_successor") is not True:
        raise AdmissionToolError(
            "invalid_invocation", "rebind requires no admitted successor"
        )
    return payload


def _verify_selected_release(
    repo_root: Path,
    selected_release_sha: str,
) -> CanonicalStateSnapshot:
    record_path = repo_root / RELEASE_RECORD_RELATIVE_PATH
    if record_path.is_symlink() or not record_path.is_file():
        raise AdmissionToolError(
            "admitted_result_rebind_failed",
            "the executing release record is absent or not an ordinary file",
        )
    try:
        record = loads_strict_json_object(record_path.read_bytes())
    except (OSError, UnicodeError, TypeError, ValueError) as exc:
        raise AdmissionToolError(
            "admitted_result_rebind_failed",
            "the executing release record is invalid",
        ) from exc
    if (
        record.get("schema_version") != RELEASE_RECORD_SCHEMA
        or record.get("release_sha") != selected_release_sha
    ):
        raise AdmissionToolError(
            "admitted_result_rebind_failed",
            "the executing release differs from the exact selected release",
        )
    loaded = load_canonical_snapshot(
        repo_root,
        source_commit=selected_release_sha,
    )
    if not loaded.ok or loaded.value is None:
        raise AdmissionToolError(
            "admitted_result_rebind_failed",
            "the selected release canonical Research State is invalid",
        )
    snapshot = loaded.value
    if type(snapshot) is not CanonicalStateSnapshot or not verify_snapshot_integrity(
        snapshot
    ):
        raise AdmissionToolError(
            "admitted_result_rebind_failed",
            "the selected release canonical snapshot failed integrity verification",
        )
    return snapshot


def _rebind_canonical_result(
    arguments: argparse.Namespace,
    repo_root: Path,
    *,
    dry_run: bool,
) -> Mapping[str, Any]:
    request = _load_rebind_request(arguments.request)
    project_id = _require_project(arguments.project_id)
    mission_id = _require_identity(arguments.mission_id, "mission_id")
    if request.get("project_id") != project_id:
        raise AdmissionToolError(
            "invalid_invocation", "request project differs from the command binding"
        )
    if request.get("mission_id") != mission_id:
        raise AdmissionToolError(
            "invalid_invocation", "request Mission differs from the command binding"
        )
    if request.get("workspace_root") != arguments.workspace_root:
        raise AdmissionToolError(
            "invalid_invocation", "request Workspace differs from the command binding"
        )
    selected_release_sha = _require_commit(
        request.get("selected_release_sha"), "selected_release_sha"
    )
    source_authority_digest = _require_digest(
        request.get("source_canonical_authority_digest"),
        "source_canonical_authority_digest",
    )
    raw_reference = request.get("admission_decision_ref")
    if not isinstance(raw_reference, Mapping) or set(raw_reference) != {
        "id",
        "digest_sha256",
    }:
        raise AdmissionToolError(
            "invalid_invocation", "admission_decision_ref has the wrong closed shape"
        )
    reference = _decision_ref(
        raw_reference.get("id"), raw_reference.get("digest_sha256")
    )
    target_snapshot = _verify_selected_release(repo_root, selected_release_sha)
    store = _open_store(arguments.workspace_root, project_id)
    _read_authorizing_decision(
        store,
        decision_ref=reference,
        mission_id=mission_id,
    )
    result = store.rebind_admitted_result(
        mission_id=mission_id,
        admission_decision_ref=reference,
        source_canonical_authority_digest=source_authority_digest,
        target_canonical_snapshot=target_snapshot,
        selected_release_sha=selected_release_sha,
        dry_run=dry_run,
    )
    if type(result) is not AdmittedResultRebindResult:
        raise AdmissionToolError(
            "admitted_result_rebind_failed",
            "the Workspace Store returned an unsupported rebind result",
        )
    return result.to_mapping()


def _dry_run_result(data: Mapping[str, Any]) -> Mapping[str, Any]:
    preview = dict(data)
    preview.update(
        {
            "dry_run": True,
            "expected_canonical_effect": "exact_authorized_delta",
            "canonical_effect": "none",
            "workspace_effect": "none",
            "mission_effect": "none",
            "strategy_effect": "none",
            "public_effect": "none",
        }
    )
    return preview


def _requested_format(raw: Sequence[str]) -> str:
    if "--json" in raw:
        return "json"
    for index, value in enumerate(raw[:-1]):
        if value == "--format" and raw[index + 1] == "json":
            return "json"
    return "text"


def main(argv: Sequence[str] | None = None) -> int:
    raw = list(sys.argv[1:] if argv is None else argv)
    if not raw:
        raw = ["usage"]
    verb = next((value for value in raw if value in _VERBS), "usage")
    output_format = _requested_format(raw)
    try:
        cleaned, repo_root, output_format, dry_run = _extract_global_options(raw)
        arguments = _parser().parse_args(cleaned)
        verb = arguments.verb
        root_selected = "--root" in raw
        if verb == "write-canonical-result" and not root_selected:
            raise AdmissionToolError(
                "invalid_invocation",
                "write-canonical-result requires an explicit clean --root worktree",
            )
        if verb == "rebind-canonical-result" and root_selected:
            raise AdmissionToolError(
                "invalid_invocation",
                "rebind-canonical-result verifies only its own executing release",
            )
        if verb == "usage":
            if dry_run:
                raise AdmissionToolError(
                    "invalid_invocation", "--dry-run applies only to effectful verbs"
                )
            _emit(_usage_contract(), output_format)
            return 0
        if verb == "write-canonical-result":
            data = _write_canonical_result(
                arguments,
                repo_root,
                dry_run=dry_run,
            )
            summary = (
                "Validated the exact Decision-derived canonical RH result without writing."
                if dry_run
                else "Wrote the exact Decision-derived canonical RH result."
            )
        else:
            data = _rebind_canonical_result(
                arguments,
                repo_root,
                dry_run=dry_run,
            )
            summary = (
                "Validated the inactive retained Workspace rebind without changing it."
                if dry_run
                else "Rebound the inactive retained Workspace to the admitted result."
            )
        if dry_run:
            data = _dry_run_result(data)
        _emit(
            _result(
                status="ok",
                verb=verb,
                summary=summary,
                data=data,
            ),
            output_format,
        )
        return 0
    except (
        AdmissionToolError,
        CanonicalAdmissionError,
        WorkspacePathError,
        WorkspaceStoreError,
        OSError,
        TypeError,
        ValueError,
    ) as exc:
        code = getattr(exc, "code", None)
        if not isinstance(code, str) or not code:
            code = (
                "canonical_result_write_failed"
                if verb == "write-canonical-result"
                else "admitted_result_rebind_failed"
                if verb == "rebind-canonical-result"
                else "invalid_invocation"
            )
        payload = _result(
            status="error",
            verb=verb,
            summary="RH Admission operator command failed without canonical effect.",
            data={
                "canonical_effect": "none",
                "mission_effect": "none",
                "strategy_effect": "none",
                "public_effect": "none",
            },
            errors=[{"code": code, "message": str(exc)}],
        )
        _emit(payload, output_format)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
