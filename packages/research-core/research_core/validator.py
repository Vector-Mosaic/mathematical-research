"""Semantic validation for canonical mathematical research state.

The validator checks epistemic and dependency invariants that JSON Schema alone
cannot express. It is intentionally standard-library only and read-only.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from types import MappingProxyType
from typing import Any, Iterable, Mapping, Sequence

from .json_support import (
    DuplicateKeyError,
    NonFiniteJSONError,
    loads_strict_json_bytes,
    loads_strict_json_object,
)


SHA256_RE = re.compile(r"^[0-9a-fA-F]{64}$")
REQUIRED_TOP_LEVEL = (
    "schema_version",
    "kind",
    "project",
    "status_model",
    "canonical_objects",
    "obligations",
    "dag",
    "claims",
    "counterexamples",
    "sources",
    "audit_receipts",
)
TOP_LEVEL_KEYS = set(REQUIRED_TOP_LEVEL)
PROJECT_KEYS = {
    "id",
    "title",
    "conjecture",
    "lifecycle_status",
    "proof_status",
    "as_of",
    "owners",
    "active_target",
    "admitted_result",
    "source_artifact_ref",
    "epistemic_rule",
}
PROJECT_PROOF_STATUSES = {"incomplete", "proved", "disproved"}
ADMITTED_RESULT_KEYS = {
    "target",
    "disposition",
    "theorem_or_counterexample_claim",
    "candidate_ref",
    "admission_decision_ref",
}
CANDIDATE_REFERENCE_KEYS = {
    "mission_id",
    "candidate_id",
    "revision",
    "digest_sha256",
}
ADMISSION_DECISION_REFERENCE_KEYS = {"decision_id", "digest_sha256"}
STATUS_MODEL_KEYS = {
    "audit_verdicts",
    "claim_types",
    "counterexample_record_kinds",
    "route_effects",
    "route_decisions",
    "receipt_kinds",
    "dag_roles",
    "lifecycle_statuses",
    "rh_chain_statuses",
    "prohibited_inputs",
}
DAG_KEYS = {"nodes", "edges", "first_dependency_gap", "immediate_target"}
CANONICAL_OBJECT_KEYS = {"id", "symbol", "definition", "domain"}
OBLIGATION_KEYS = {
    "id",
    "label",
    "statement",
    "proof_status",
    "lifecycle_status",
    "requires",
}
DAG_NODE_KEYS = {"id", "label", "status"}
DAG_EDGE_KEYS = {
    "id",
    "from",
    "to",
    "relation",
    "status",
    "claim_ref",
    "blocks_rh_chain",
}
CLAIM_KEYS = {
    "id",
    "title",
    "statement",
    "hypotheses",
    "status",
    "claim_type",
    "audit_verdict",
    "dag_role",
    "rh_chain_status",
    "depends_on",
    "implies",
    "citation_refs",
    "evidence_refs",
    "dag_node_refs",
    "dag_edge_refs",
    "proof_restrictions",
    "revival_trigger",
    "refutation",
}
COUNTEREXAMPLE_KEYS = {
    "id",
    "title",
    "record_kind",
    "exact_scope",
    "basis",
    "route_effect",
    "does_not_exclude",
    "revival_trigger",
    "statement",
    "disposition",
    "status",
    "audit_verdict",
    "route",
    "proof_restrictions",
    "evidence_refs",
}
SOURCE_KEYS = {
    "id",
    "title",
    "authors",
    "source_type",
    "path_class",
    "locator",
    "original_path",
    "url",
    "sha256",
}
AUDIT_RECEIPT_KEYS = {
    "id",
    "title",
    "receipt_kind",
    "campaign_id",
    "date",
    "verdict",
    "source_refs",
    "claim_refs",
    "evidence_refs",
    "route_dispositions",
    "tracked_receipt_path",
    "conclusion",
}
REQUIRED_PROHIBITED_INPUTS = {
    "riemann_hypothesis",
    "zeta_zero_expansion",
    "equivalent_target_conclusion",
}
AUDIT_VERDICTS = {
    "verified",
    "verified_with_conditions",
    "unproved_gap",
    "false",
    "duplicate_downstream_only",
    "not_audited",
}
CLAIM_TYPES = {
    "known_theorem",
    "exact_identity",
    "proved_project_lemma",
    "candidate_bridge",
    "finite_certificate",
    "numerical_observation",
    "scoped_method_counterexample",
    "paused_branch",
}
DAG_ROLES = {
    "certified_chain",
    "first_dependency_gap",
    "immediate_target",
    "supporting",
    "diagnostic",
    "excluded",
}
LIFECYCLE_STATUSES = {"active", "paused", "superseded", "archived"}
RH_CHAIN_STATUSES = {"usable", "conditional_only", "diagnostic", "excluded"}
BLOCKING_VERDICTS = {"false", "duplicate_downstream_only", "unproved_gap"}
NON_ALL_ORDER_TYPES = {"finite_certificate", "numerical_observation"}
COUNTEREXAMPLE_KINDS = {
    "exact_counterexample",
    "mechanism_exclusion",
    "historical_non_gate",
}
RECEIPT_KINDS = {
    "audit",
    "target_refinement",
    "campaign_wave",
    "campaign_closeout",
    "process_audit",
}
ROUTE_DECISIONS = {"continue", "pause", "exclude", "supersede"}
ROUTE_EFFECTS = {"exclude_within_scope", "no_inference"}
ROUTE_DISPOSITION_KEYS = {
    "decision",
    "scope",
    "basis",
    "evidence_refs",
    "truth_effect",
    "non_inferences",
    "revival_trigger",
}

REPO_ROOT = Path(__file__).resolve().parents[3]
STATE_SCHEMA_PATH = (
    REPO_ROOT
    / "contracts"
    / "schemas"
    / "research_state.schema.json"
)


def _schema_type_matches(value: Any, expected: str) -> bool:
    if expected == "object":
        return isinstance(value, Mapping)
    if expected == "array":
        return isinstance(value, (list, tuple))
    if expected == "string":
        return isinstance(value, str)
    if expected == "boolean":
        return isinstance(value, bool)
    if expected == "null":
        return value is None
    if expected == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if expected == "number":
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    return False


def _resolve_local_schema_ref(root: Mapping[str, Any], ref: str) -> Mapping[str, Any]:
    if not ref.startswith("#/"):
        raise ValueError(f"unsupported schema reference: {ref}")
    value: Any = root
    for raw_part in ref[2:].split("/"):
        part = raw_part.replace("~1", "/").replace("~0", "~")
        if not isinstance(value, Mapping) or part not in value:
            raise ValueError(f"unresolved schema reference: {ref}")
        value = value[part]
    if not isinstance(value, Mapping):
        raise ValueError(f"schema reference is not an object: {ref}")
    return value


def _matches_present_discriminators(
    value: Any,
    schema: Mapping[str, Any],
) -> bool | None:
    """Return whether a branch matches its present const/enum discriminators."""

    if not isinstance(value, Mapping):
        return None
    properties = schema.get("properties")
    if not isinstance(properties, Mapping):
        return None
    found = False
    for key, child_schema in properties.items():
        if key not in value or not isinstance(child_schema, Mapping):
            continue
        if "const" in child_schema:
            found = True
            if value[key] != child_schema["const"]:
                return False
        enum = child_schema.get("enum")
        if isinstance(enum, list):
            found = True
            if value[key] not in enum:
                return False
    return True if found else None


def _schema_violations(
    value: Any,
    schema: Mapping[str, Any],
    root: Mapping[str, Any],
    location: str,
) -> list[tuple[str, str]]:
    """Evaluate the JSON-Schema vocabulary used by canonical research contracts."""

    violations: list[tuple[str, str]] = []
    reference = schema.get("$ref")
    if isinstance(reference, str):
        violations.extend(
            _schema_violations(
                value, _resolve_local_schema_ref(root, reference), root, location
            )
        )

    expected_type = schema.get("type")
    if isinstance(expected_type, str) and not _schema_type_matches(value, expected_type):
        return violations + [(location, f"must have JSON type {expected_type}")]

    if "const" in schema and value != schema["const"]:
        violations.append((location, f"must equal schema constant {schema['const']!r}"))
    enum = schema.get("enum")
    if isinstance(enum, list) and value not in enum:
        violations.append((location, "value is not in the schema enum"))

    all_of = schema.get("allOf")
    if isinstance(all_of, list):
        for branch in all_of:
            if isinstance(branch, Mapping):
                violations.extend(_schema_violations(value, branch, root, location))

    any_of = schema.get("anyOf")
    if isinstance(any_of, list):
        alternatives = [branch for branch in any_of if isinstance(branch, Mapping)]
        if not any(
            not _schema_violations(value, branch, root, location)
            for branch in alternatives
        ):
            violations.append((location, "must match at least one schema alternative"))

    negation = schema.get("not")
    if isinstance(negation, Mapping) and not _schema_violations(
        value, negation, root, location
    ):
        violations.append((location, "must not match the forbidden schema shape"))

    one_of = schema.get("oneOf")
    if isinstance(one_of, list):
        branches = [branch for branch in one_of if isinstance(branch, Mapping)]
        branch_failures: list[list[tuple[str, str]]] = []
        for branch in branches:
            branch_failures.append(
                _schema_violations(value, branch, root, location)
            )
        successes = sum(not failure for failure in branch_failures)
        if successes == 0 and branch_failures:
            discriminator_matches = [
                index
                for index, branch in enumerate(branches)
                if _matches_present_discriminators(value, branch) is True
            ]
            if len(discriminator_matches) == 1:
                closest = [branch_failures[discriminator_matches[0]]]
            else:
                fewest = min(len(failure) for failure in branch_failures)
                closest = [
                    failure for failure in branch_failures if len(failure) == fewest
                ]
            if len(closest) == 1 and closest[0]:
                deepest = max(
                    closest[0],
                    key=lambda item: (item[0].count(".") + item[0].count("[")),
                )
                violations.append(deepest)
            else:
                violations.append(
                    (location, "must match exactly one schema alternative")
                )
        elif successes != 1:
            violations.append((location, "must match exactly one schema alternative"))

    condition = schema.get("if")
    if isinstance(condition, Mapping) and not _schema_violations(
        value, condition, root, location
    ):
        consequence = schema.get("then")
        if isinstance(consequence, Mapping):
            violations.extend(_schema_violations(value, consequence, root, location))

    if isinstance(value, Mapping):
        minimum = schema.get("minProperties")
        if isinstance(minimum, int) and len(value) < minimum:
            violations.append((location, f"must contain at least {minimum} property/properties"))
        required = schema.get("required")
        if isinstance(required, list):
            for key in required:
                if isinstance(key, str) and key not in value:
                    violations.append((location, f"missing required schema field: {key}"))
        properties = schema.get("properties")
        declared = properties if isinstance(properties, Mapping) else {}
        property_names = schema.get("propertyNames")
        if isinstance(property_names, Mapping):
            for key in value:
                violations.extend(
                    _schema_violations(key, property_names, root, f"{location}.{key}")
                )
        additional = schema.get("additionalProperties")
        if additional is False:
            for key in sorted(set(value) - set(declared)):
                violations.append((f"{location}.{key}", "unknown field under schema"))
        elif isinstance(additional, Mapping):
            for key in sorted(set(value) - set(declared)):
                violations.extend(
                    _schema_violations(value[key], additional, root, f"{location}.{key}")
                )
        for key, child_schema in declared.items():
            if key in value and isinstance(child_schema, Mapping):
                violations.extend(
                    _schema_violations(
                        value[key], child_schema, root, f"{location}.{key}"
                    )
                )

    if isinstance(value, (list, tuple)):
        minimum = schema.get("minItems")
        if isinstance(minimum, int) and len(value) < minimum:
            violations.append((location, f"must contain at least {minimum} item(s)"))
        maximum = schema.get("maxItems")
        if isinstance(maximum, int) and len(value) > maximum:
            violations.append((location, f"must contain at most {maximum} item(s)"))
        if schema.get("uniqueItems") is True:
            for index, item in enumerate(value):
                if any(item == previous for previous in value[:index]):
                    violations.append((location, "must not contain duplicate items"))
                    break
        item_schema = schema.get("items")
        if isinstance(item_schema, Mapping):
            for index, item in enumerate(value):
                violations.extend(
                    _schema_violations(
                        item, item_schema, root, f"{location}[{index}]"
                    )
                )

    if isinstance(value, str):
        minimum = schema.get("minLength")
        if isinstance(minimum, int) and len(value) < minimum:
            violations.append((location, f"must have length at least {minimum}"))
        pattern = schema.get("pattern")
        if isinstance(pattern, str) and re.search(pattern, value) is None:
            violations.append((location, f"must match schema pattern {pattern}"))
        if schema.get("format") == "date":
            try:
                parsed = date.fromisoformat(value)
            except ValueError:
                parsed = None
            if parsed is None or parsed.isoformat() != value:
                violations.append((location, "must be an ISO 8601 calendar date"))

    if isinstance(value, (int, float)) and not isinstance(value, bool):
        minimum = schema.get("minimum")
        if isinstance(minimum, (int, float)) and value < minimum:
            violations.append((location, f"must be at least {minimum}"))

    return violations


def _load_state_schema() -> Mapping[str, Any]:
    schema = loads_strict_json_object(STATE_SCHEMA_PATH.read_bytes())
    return schema


def _validate_schema_parity(state: Mapping[str, Any]) -> tuple[Finding, ...]:
    try:
        schema = _load_state_schema()
        violations = _schema_violations(state, schema, schema, "$")
    except (OSError, UnicodeDecodeError, ValueError, TypeError) as exc:
        return (Finding("ERROR", str(STATE_SCHEMA_PATH), f"unable to enforce schema: {exc}"),)
    return tuple(
        Finding("ERROR", location, f"Research State schema: {message}")
        for location, message in violations
    )


@dataclass(frozen=True)
class Finding:
    severity: str
    location: str
    message: str

    def to_dict(self) -> dict[str, str]:
        return {
            "severity": self.severity,
            "location": self.location,
            "message": self.message,
        }


@dataclass(frozen=True)
class ValidationResult:
    findings: tuple[Finding, ...]
    counts: Mapping[str, int]

    def __post_init__(self) -> None:
        object.__setattr__(self, "findings", tuple(self.findings))
        object.__setattr__(self, "counts", MappingProxyType(dict(self.counts)))

    @property
    def errors(self) -> tuple[Finding, ...]:
        return tuple(item for item in self.findings if item.severity == "ERROR")

    @property
    def ok(self) -> bool:
        return not self.errors


def _error(findings: list[Finding], location: str, message: str) -> None:
    findings.append(Finding("ERROR", location, message))


def _exact_keys(
    value: Mapping[str, Any], allowed: set[str], location: str, findings: list[Finding]
) -> None:
    """Reject authority aliases and undeclared structural families."""

    for key in sorted(set(value) - allowed):
        _error(findings, f"{location}.{key}", "unknown key for Research State v2")


def _records(
    state: Mapping[str, Any], key: str, findings: list[Finding]
) -> list[Mapping[str, Any]]:
    value = state.get(key)
    if not isinstance(value, list):
        _error(findings, key, "must be an array")
        return []
    records: list[Mapping[str, Any]] = []
    for index, item in enumerate(value):
        if not isinstance(item, Mapping):
            _error(findings, f"{key}[{index}]", "must be an object")
            continue
        records.append(item)
    return records


def _id_index(
    records: Sequence[Mapping[str, Any]], family: str, findings: list[Finding]
) -> dict[str, Mapping[str, Any]]:
    result: dict[str, Mapping[str, Any]] = {}
    for index, record in enumerate(records):
        record_id = record.get("id")
        location = f"{family}[{index}]"
        if not isinstance(record_id, str) or not record_id.strip():
            _error(findings, location, "id must be a non-empty string")
            continue
        if record_id in result:
            _error(findings, location, f"duplicate id within {family}: {record_id}")
            continue
        result[record_id] = record
    return result


def _string_list(
    value: Any, location: str, findings: list[Finding], *, required: bool = True
) -> list[str]:
    if value is None and not required:
        return []
    if not isinstance(value, list) or not all(
        isinstance(item, str) and item.strip() for item in value
    ):
        _error(findings, location, "must be an array of non-empty strings")
        return []
    if len(value) != len(set(value)):
        _error(findings, location, "must not contain duplicate values")
    return list(value)


def _check_refs(
    refs: Iterable[str], allowed: set[str], location: str, findings: list[Finding]
) -> None:
    for ref in refs:
        if ref not in allowed:
            _error(findings, location, f"unresolved reference: {ref}")


def _nonempty_string(
    value: Any, location: str, findings: list[Finding]
) -> str:
    if not isinstance(value, str) or not value.strip():
        _error(findings, location, "must be a non-empty string")
        return ""
    return value


def _validate_refutation(
    value: Any,
    *,
    location: str,
    evidence_ids: set[str],
    proof_evidence_ids: set[str],
    proof_anchor_ids: set[str],
    self_id: str,
    findings: list[Finding],
) -> None:
    expected = {
        "kind",
        "exact_statement",
        "exact_scope",
        "evidence_refs",
        "does_not_exclude",
    }
    if not isinstance(value, Mapping):
        _error(
            findings,
            location,
            "false verdict requires a proof-carrying refutation object",
        )
        return
    for key in sorted(set(value) - expected):
        _error(findings, f"{location}.{key}", "unknown refutation key")
    for key in sorted(expected - set(value)):
        _error(findings, location, f"missing required refutation key: {key}")
    if value.get("kind") not in {"exact_counterexample", "proof_of_negation"}:
        _error(
            findings,
            f"{location}.kind",
            "must be exact_counterexample or proof_of_negation",
        )
    _nonempty_string(
        value.get("exact_statement"), f"{location}.exact_statement", findings
    )
    _nonempty_string(value.get("exact_scope"), f"{location}.exact_scope", findings)
    evidence = _string_list(
        value.get("evidence_refs"), f"{location}.evidence_refs", findings
    )
    if not evidence:
        _error(
            findings,
            f"{location}.evidence_refs",
            "must contain proof or exact-counterexample evidence",
        )
    _check_refs(evidence, evidence_ids, f"{location}.evidence_refs", findings)
    if self_id in evidence:
        _error(
            findings,
            f"{location}.evidence_refs",
            "a refuted claim cannot cite itself as refutation evidence",
        )
    for ref in evidence:
        if ref in evidence_ids and ref not in proof_evidence_ids:
            _error(
                findings,
                f"{location}.evidence_refs",
                f"reference is not an eligible proof-evidence record: {ref}",
            )
    if evidence and not set(evidence) & proof_anchor_ids:
        _error(
            findings,
            f"{location}.evidence_refs",
            "refutation requires an independently audited mathematical evidence anchor",
        )
    non_inferences = _string_list(
        value.get("does_not_exclude"),
        f"{location}.does_not_exclude",
        findings,
    )
    if not non_inferences:
        _error(
            findings,
            f"{location}.does_not_exclude",
            "must state at least one non-inference boundary",
        )


def _validate_route_disposition(
    value: Any,
    *,
    location: str,
    allowed_decisions: set[str],
    evidence_ids: set[str],
    route_evidence_ids: set[str],
    exclusion_gate_ids: set[str],
    receipt_id: str,
    findings: list[Finding],
) -> None:
    if not isinstance(value, Mapping):
        _error(findings, location, "must be an object")
        return
    for key in sorted(set(value) - ROUTE_DISPOSITION_KEYS):
        _error(findings, f"{location}.{key}", "unknown route-disposition key")
    for key in sorted(ROUTE_DISPOSITION_KEYS - set(value)):
        _error(findings, location, f"missing required route-disposition key: {key}")
    decision = value.get("decision")
    if decision not in allowed_decisions:
        _error(
            findings,
            f"{location}.decision",
            f"value is not declared in status_model.route_decisions: {decision}",
        )
    _nonempty_string(value.get("scope"), f"{location}.scope", findings)
    _nonempty_string(value.get("basis"), f"{location}.basis", findings)
    evidence = _string_list(
        value.get("evidence_refs"), f"{location}.evidence_refs", findings
    )
    if not evidence:
        _error(
            findings,
            f"{location}.evidence_refs",
            "must cite canonical evidence for the route decision",
        )
    _check_refs(evidence, evidence_ids, f"{location}.evidence_refs", findings)
    if receipt_id in evidence:
        _error(
            findings,
            f"{location}.evidence_refs",
            "a route disposition cannot cite its containing receipt as evidence",
        )
    for ref in evidence:
        if ref in evidence_ids and ref not in route_evidence_ids:
            _error(
                findings,
                f"{location}.evidence_refs",
                f"reference is not an eligible route-evidence record: {ref}",
            )
    truth_effect = value.get("truth_effect")
    if truth_effect not in {"none", "scoped_exclusion"}:
        _error(
            findings,
            f"{location}.truth_effect",
            "must be none or scoped_exclusion",
        )
    non_inferences = _string_list(
        value.get("non_inferences"), f"{location}.non_inferences", findings
    )
    if not non_inferences:
        _error(
            findings,
            f"{location}.non_inferences",
            "must state at least one non-inference boundary",
        )
    revival_trigger = value.get("revival_trigger")
    if revival_trigger is not None and (
        not isinstance(revival_trigger, str) or not revival_trigger.strip()
    ):
        _error(
            findings,
            f"{location}.revival_trigger",
            "must be null or a non-empty string",
        )
    if decision == "pause":
        if truth_effect != "none":
            _error(
                findings,
                f"{location}.truth_effect",
                "pause is non-refuting and must have truth_effect none",
            )
        if not isinstance(revival_trigger, str) or not revival_trigger.strip():
            _error(
                findings,
                f"{location}.revival_trigger",
                "pause requires a non-empty revival trigger",
            )
    elif decision in {"continue", "supersede"} and truth_effect != "none":
        _error(
            findings,
            f"{location}.truth_effect",
            f"{decision} is non-refuting and must have truth_effect none",
        )
    elif decision == "exclude" and truth_effect != "scoped_exclusion":
        _error(
            findings,
            f"{location}.truth_effect",
            "exclude requires a proof-carrying scoped_exclusion",
        )
    if decision == "exclude" and not set(evidence) & exclusion_gate_ids:
        _error(
            findings,
            f"{location}.evidence_refs",
            "exclude must cite an active scoped gate or a refuted claim",
        )


def validate_state(state: Mapping[str, Any]) -> ValidationResult:
    """Validate one parsed research-state document."""

    findings: list[Finding] = list(_validate_schema_parity(state))
    _exact_keys(state, TOP_LEVEL_KEYS, "$", findings)
    for key in REQUIRED_TOP_LEVEL:
        if key not in state:
            _error(findings, "$", f"missing required top-level field: {key}")

    if state.get("schema_version") != 2:
        _error(findings, "schema_version", "must equal 2")
    if state.get("kind") != "mathematical_research_state":
        _error(findings, "kind", "must equal mathematical_research_state")

    project = state.get("project")
    if not isinstance(project, Mapping):
        _error(findings, "project", "must be an object")
        project = {}
    _exact_keys(project, PROJECT_KEYS, "project", findings)
    for key in ("id", "title", "lifecycle_status", "proof_status", "as_of"):
        if not isinstance(project.get(key), str) or not project.get(key):
            _error(findings, f"project.{key}", "must be a non-empty string")
    proof_status = project.get("proof_status")
    if not isinstance(proof_status, str) or proof_status not in PROJECT_PROOF_STATUSES:
        _error(
            findings,
            "project.proof_status",
            "must be exactly incomplete, proved, or disproved",
        )
    admitted_result = project.get("admitted_result")
    if proof_status == "incomplete":
        if "admitted_result" in project:
            _error(
                findings,
                "project.admitted_result",
                "must be absent while project.proof_status is incomplete",
            )
        if "active_target" not in project:
            _error(
                findings,
                "project.active_target",
                "is required while project.proof_status is incomplete",
            )
    elif isinstance(proof_status, str) and proof_status in {"proved", "disproved"}:
        if "active_target" in project:
            _error(
                findings,
                "project.active_target",
                "must be absent after a canonical proof or disproof is admitted",
            )
        if not isinstance(admitted_result, Mapping):
            _error(
                findings,
                "project.admitted_result",
                "must be an object for a proved or disproved project",
            )
        else:
            _exact_keys(
                admitted_result,
                ADMITTED_RESULT_KEYS,
                "project.admitted_result",
                findings,
            )
            if admitted_result.get("target") != "riemann_hypothesis":
                _error(
                    findings,
                    "project.admitted_result.target",
                    "must equal riemann_hypothesis",
                )
            if admitted_result.get("disposition") != proof_status:
                _error(
                    findings,
                    "project.admitted_result.disposition",
                    "must exactly match project.proof_status",
                )
            _nonempty_string(
                admitted_result.get("theorem_or_counterexample_claim"),
                "project.admitted_result.theorem_or_counterexample_claim",
                findings,
            )
            candidate_ref = admitted_result.get("candidate_ref")
            if not isinstance(candidate_ref, Mapping):
                _error(
                    findings,
                    "project.admitted_result.candidate_ref",
                    "must be an exact Candidate reference object",
                )
            else:
                _exact_keys(
                    candidate_ref,
                    CANDIDATE_REFERENCE_KEYS,
                    "project.admitted_result.candidate_ref",
                    findings,
                )
                for key in ("mission_id", "candidate_id"):
                    _nonempty_string(
                        candidate_ref.get(key),
                        f"project.admitted_result.candidate_ref.{key}",
                        findings,
                    )
                revision = candidate_ref.get("revision")
                if (
                    not isinstance(revision, int)
                    or isinstance(revision, bool)
                    or revision < 1
                ):
                    _error(
                        findings,
                        "project.admitted_result.candidate_ref.revision",
                        "must be an integer greater than or equal to 1",
                    )
                digest = candidate_ref.get("digest_sha256")
                if not isinstance(digest, str) or not SHA256_RE.fullmatch(digest):
                    _error(
                        findings,
                        "project.admitted_result.candidate_ref.digest_sha256",
                        "must be a 64-character hexadecimal SHA-256",
                    )
            decision_ref = admitted_result.get("admission_decision_ref")
            if not isinstance(decision_ref, Mapping):
                _error(
                    findings,
                    "project.admitted_result.admission_decision_ref",
                    "must be an exact Admission Decision reference object",
                )
            else:
                _exact_keys(
                    decision_ref,
                    ADMISSION_DECISION_REFERENCE_KEYS,
                    "project.admitted_result.admission_decision_ref",
                    findings,
                )
                _nonempty_string(
                    decision_ref.get("decision_id"),
                    "project.admitted_result.admission_decision_ref.decision_id",
                    findings,
                )
                digest = decision_ref.get("digest_sha256")
                if not isinstance(digest, str) or not SHA256_RE.fullmatch(digest):
                    _error(
                        findings,
                        "project.admitted_result.admission_decision_ref.digest_sha256",
                        "must be a 64-character hexadecimal SHA-256",
                    )

    status_model = state.get("status_model")
    if not isinstance(status_model, Mapping):
        _error(findings, "status_model", "must be an object")
        status_model = {}
    _exact_keys(status_model, STATUS_MODEL_KEYS, "status_model", findings)
    enum_fields = {
        "audit_verdict": _string_list(
            status_model.get("audit_verdicts"),
            "status_model.audit_verdicts",
            findings,
        ),
        "claim_type": _string_list(
            status_model.get("claim_types"), "status_model.claim_types", findings
        ),
        "dag_role": _string_list(
            status_model.get("dag_roles"), "status_model.dag_roles", findings
        ),
        "status": _string_list(
            status_model.get("lifecycle_statuses"),
            "status_model.lifecycle_statuses",
            findings,
        ),
        "rh_chain_status": _string_list(
            status_model.get("rh_chain_statuses"),
            "status_model.rh_chain_statuses",
            findings,
        ),
    }
    declared_counterexample_kinds = set(
        _string_list(
            status_model.get("counterexample_record_kinds"),
            "status_model.counterexample_record_kinds",
            findings,
        )
    )
    declared_route_effects = set(
        _string_list(
            status_model.get("route_effects"),
            "status_model.route_effects",
            findings,
        )
    )
    declared_route_decisions = set(
        _string_list(
            status_model.get("route_decisions"),
            "status_model.route_decisions",
            findings,
        )
    )
    declared_receipt_kinds = set(
        _string_list(
            status_model.get("receipt_kinds"),
            "status_model.receipt_kinds",
            findings,
        )
    )
    prohibited_inputs = set(
        _string_list(
            status_model.get("prohibited_inputs"),
            "status_model.prohibited_inputs",
            findings,
        )
    )
    declared_vocabularies = (
        ("audit_verdicts", set(enum_fields["audit_verdict"]), AUDIT_VERDICTS),
        ("claim_types", set(enum_fields["claim_type"]), CLAIM_TYPES),
        ("dag_roles", set(enum_fields["dag_role"]), DAG_ROLES),
        ("lifecycle_statuses", set(enum_fields["status"]), LIFECYCLE_STATUSES),
        (
            "rh_chain_statuses",
            set(enum_fields["rh_chain_status"]),
            RH_CHAIN_STATUSES,
        ),
        (
            "counterexample_record_kinds",
            declared_counterexample_kinds,
            COUNTEREXAMPLE_KINDS,
        ),
        ("route_effects", declared_route_effects, ROUTE_EFFECTS),
        ("route_decisions", declared_route_decisions, ROUTE_DECISIONS),
        ("receipt_kinds", declared_receipt_kinds, RECEIPT_KINDS),
        ("prohibited_inputs", prohibited_inputs, REQUIRED_PROHIBITED_INPUTS),
    )
    for field, declared, expected in declared_vocabularies:
        if declared != expected:
            _error(
                findings,
                f"status_model.{field}",
                "must declare exactly: " + ", ".join(sorted(expected)),
            )
    objects = _records(state, "canonical_objects", findings)
    obligations = _records(state, "obligations", findings)
    claims = _records(state, "claims", findings)
    counterexamples = _records(state, "counterexamples", findings)
    sources = _records(state, "sources", findings)
    receipts = _records(state, "audit_receipts", findings)

    dag = state.get("dag")
    if not isinstance(dag, Mapping):
        _error(findings, "dag", "must be an object")
        dag = {}
    _exact_keys(dag, DAG_KEYS, "dag", findings)
    nodes_value = dag.get("nodes")
    edges_value = dag.get("edges")
    nodes = list(nodes_value) if isinstance(nodes_value, list) else []
    edges = list(edges_value) if isinstance(edges_value, list) else []
    if not isinstance(nodes_value, list):
        _error(findings, "dag.nodes", "must be an array")
    if not isinstance(edges_value, list):
        _error(findings, "dag.edges", "must be an array")
    nodes = [item for item in nodes if isinstance(item, Mapping)]
    edges = [item for item in edges if isinstance(item, Mapping)]

    record_key_contracts = (
        ("canonical_objects", objects, CANONICAL_OBJECT_KEYS),
        ("obligations", obligations, OBLIGATION_KEYS),
        ("dag.nodes", nodes, DAG_NODE_KEYS),
        ("dag.edges", edges, DAG_EDGE_KEYS),
        ("claims", claims, CLAIM_KEYS),
        ("counterexamples", counterexamples, COUNTEREXAMPLE_KEYS),
        ("sources", sources, SOURCE_KEYS),
        ("audit_receipts", receipts, AUDIT_RECEIPT_KEYS),
    )
    for family, records, allowed_keys in record_key_contracts:
        for index, record in enumerate(records):
            _exact_keys(record, allowed_keys, f"{family}[{index}]", findings)

    families = {
        "canonical_objects": _id_index(objects, "canonical_objects", findings),
        "obligations": _id_index(obligations, "obligations", findings),
        "dag.nodes": _id_index(nodes, "dag.nodes", findings),
        "dag.edges": _id_index(edges, "dag.edges", findings),
        "claims": _id_index(claims, "claims", findings),
        "counterexamples": _id_index(counterexamples, "counterexamples", findings),
        "sources": _id_index(sources, "sources", findings),
        "audit_receipts": _id_index(receipts, "audit_receipts", findings),
    }
    global_seen: dict[str, str] = {}
    for family, index in families.items():
        for record_id in index:
            previous = global_seen.get(record_id)
            if previous:
                _error(
                    findings,
                    family,
                    f"id {record_id} also appears in {previous}; ids must be globally unique",
                )
            else:
                global_seen[record_id] = family

    object_ids = set(families["canonical_objects"])
    obligation_ids = set(families["obligations"])
    node_ids = set(families["dag.nodes"])
    edge_ids = set(families["dag.edges"])
    claim_ids = set(families["claims"])
    counterexample_ids = set(families["counterexamples"])
    source_ids = set(families["sources"])
    receipt_ids = set(families["audit_receipts"])
    evidence_ids = object_ids | obligation_ids | node_ids | edge_ids | claim_ids | counterexample_ids | source_ids | receipt_ids
    audited_claim_ids = {
        str(claim.get("id"))
        for claim in claims
        if claim.get("audit_verdict") in {"verified", "verified_with_conditions"}
    }
    false_claim_gate_ids = {
        str(claim.get("id"))
        for claim in claims
        if claim.get("audit_verdict") == "false"
        and isinstance(claim.get("refutation"), Mapping)
    }
    audited_counterexample_ids = {
        str(counterexample.get("id"))
        for counterexample in counterexamples
        if counterexample.get("audit_verdict")
        in {"verified", "verified_with_conditions"}
    }
    active_gate_ids = {
        str(counterexample.get("id"))
        for counterexample in counterexamples
        if counterexample.get("status") == "active"
        and counterexample.get("audit_verdict")
        in {"verified", "verified_with_conditions"}
        and counterexample.get("record_kind")
        in {"exact_counterexample", "mechanism_exclusion"}
        and counterexample.get("route_effect") == "exclude_within_scope"
    }
    mathematical_receipt_ids = {
        str(receipt.get("id"))
        for receipt in receipts
        if receipt.get("receipt_kind")
        in {"audit", "target_refinement", "campaign_wave", "campaign_closeout"}
    }
    active_gate_proof_evidence_ids = (
        source_ids | mathematical_receipt_ids | audited_claim_ids
    )
    active_gate_proof_anchor_ids = mathematical_receipt_ids | audited_claim_ids
    refutation_proof_evidence_ids = (
        active_gate_proof_evidence_ids | active_gate_ids
    )
    refutation_proof_anchor_ids = active_gate_proof_anchor_ids | active_gate_ids
    route_evidence_ids = (
        receipt_ids
        | audited_claim_ids
        | false_claim_gate_ids
        | audited_counterexample_ids
    )
    exclusion_gate_ids = active_gate_ids | false_claim_gate_ids

    active_target_ref = project.get("active_target")
    if active_target_ref is not None and active_target_ref not in claim_ids:
        _error(findings, "project.active_target", f"references unknown claim: {active_target_ref}")
    source_artifact_ref = project.get("source_artifact_ref")
    if source_artifact_ref is not None and source_artifact_ref not in source_ids:
        _error(
            findings,
            "project.source_artifact_ref",
            f"references unknown source: {source_artifact_ref}",
        )

    for index, obligation in enumerate(obligations):
        location = f"obligations[{index}]"
        if not isinstance(obligation.get("proof_status"), str):
            _error(findings, location, "proof_status must be a string")
        if not isinstance(obligation.get("lifecycle_status"), str):
            _error(findings, location, "lifecycle_status must be a string")
        _check_refs(
            _string_list(
                obligation.get("requires"),
                f"{location}.requires",
                findings,
                required=False,
            ),
            obligation_ids,
            f"{location}.requires",
            findings,
        )

    for required_id in ("obligation.B0", "obligation.B1"):
        record = families["obligations"].get(required_id)
        if record is None:
            _error(findings, "obligations", f"missing required obligation: {required_id}")
        elif record.get("proof_status") not in {"unproved", "unproved_gap"} or record.get("lifecycle_status") != "active":
            _error(
                findings,
                required_id,
                "must remain active and unproved until a general proof is audited",
            )

    gap_edges: list[Mapping[str, Any]] = []
    for index, edge in enumerate(edges):
        location = f"dag.edges[{index}]"
        edge_from = edge.get("from")
        edge_to = edge.get("to")
        if not isinstance(edge_from, str) or edge_from not in node_ids:
            _error(findings, location, f"from references unknown DAG node: {edge_from}")
        if not isinstance(edge_to, str) or edge_to not in node_ids:
            _error(findings, location, f"to references unknown DAG node: {edge_to}")
        claim_ref = edge.get("claim_ref")
        if claim_ref is not None and claim_ref not in claim_ids:
            _error(findings, location, f"claim_ref references unknown claim: {claim_ref}")
        if edge.get("status") == "unproved_gap":
            gap_edges.append(edge)
    if len(gap_edges) != 1:
        _error(findings, "dag.edges", "exactly one edge must have status unproved_gap")
    first_gap = dag.get("first_dependency_gap")
    if first_gap not in edge_ids:
        _error(findings, "dag.first_dependency_gap", "must reference a DAG edge")
    elif len(gap_edges) == 1 and gap_edges[0].get("id") != first_gap:
        _error(findings, "dag.first_dependency_gap", "must identify the sole unproved-gap edge")

    immediate_claims: list[Mapping[str, Any]] = []
    for index, claim in enumerate(claims):
        location = f"claims[{index}]"
        for field, allowed_values in enum_fields.items():
            value = claim.get(field)
            if not isinstance(value, str):
                _error(findings, f"{location}.{field}", "must be a string")
            elif allowed_values and value not in allowed_values:
                _error(findings, f"{location}.{field}", f"value is not declared in status_model: {value}")

        depends_on = _string_list(claim.get("depends_on"), f"{location}.depends_on", findings)
        implies = _string_list(claim.get("implies"), f"{location}.implies", findings)
        citations = _string_list(claim.get("citation_refs"), f"{location}.citation_refs", findings)
        evidence = _string_list(claim.get("evidence_refs"), f"{location}.evidence_refs", findings)
        restrictions = _string_list(
            claim.get("proof_restrictions"), f"{location}.proof_restrictions", findings
        )
        _check_refs(depends_on, claim_ids, f"{location}.depends_on", findings)
        _check_refs(implies, claim_ids | obligation_ids | node_ids, f"{location}.implies", findings)
        _check_refs(citations, source_ids, f"{location}.citation_refs", findings)
        _check_refs(evidence, evidence_ids, f"{location}.evidence_refs", findings)
        _check_refs(
            _string_list(claim.get("dag_node_refs"), f"{location}.dag_node_refs", findings, required=False),
            node_ids,
            f"{location}.dag_node_refs",
            findings,
        )
        _check_refs(
            _string_list(claim.get("dag_edge_refs"), f"{location}.dag_edge_refs", findings, required=False),
            edge_ids,
            f"{location}.dag_edge_refs",
            findings,
        )
        if claim.get("claim_type") in NON_ALL_ORDER_TYPES and "all_order_exclusion" not in restrictions:
            _error(findings, location, "finite or numerical claims require all_order_exclusion")
        if claim.get("claim_type") == "candidate_bridge":
            implication_set = set(implies)
            if "obligation.stieltjes" not in implication_set and not {
                "obligation.B0",
                "obligation.B1",
            }.issubset(implication_set):
                _error(
                    findings,
                    location,
                    "candidate_bridge must imply Stieltjes or both B0 and B1 obligations",
                )
        if claim.get("audit_verdict") in BLOCKING_VERDICTS and claim.get("rh_chain_status") == "usable":
            _error(findings, location, "a blocking verdict cannot be RH-chain usable")
        if claim.get("status") == "paused":
            _nonempty_string(
                claim.get("revival_trigger"),
                f"{location}.revival_trigger",
                findings,
            )
        if claim.get("audit_verdict") == "false":
            _validate_refutation(
                claim.get("refutation"),
                location=f"{location}.refutation",
                evidence_ids=evidence_ids,
                proof_evidence_ids=refutation_proof_evidence_ids,
                proof_anchor_ids=refutation_proof_anchor_ids,
                self_id=str(claim.get("id") or ""),
                findings=findings,
            )
        if claim.get("dag_role") == "immediate_target" and claim.get("status") == "active":
            immediate_claims.append(claim)

    if len(immediate_claims) != 1:
        _error(findings, "claims", "exactly one active claim must have dag_role immediate_target")
    immediate_target = dag.get("immediate_target")
    if immediate_target not in claim_ids:
        _error(findings, "dag.immediate_target", "must reference a claim")
    elif len(immediate_claims) == 1:
        target = immediate_claims[0]
        if target.get("id") != immediate_target:
            _error(findings, "dag.immediate_target", "must reference the sole active immediate target")
        if target.get("audit_verdict") != "unproved_gap":
            _error(findings, "dag.immediate_target", "immediate target must remain unproved_gap")
        target_implications = set(target.get("implies") or [])
        if not {"obligation.B0", "obligation.B1"}.issubset(target_implications):
            _error(findings, "dag.immediate_target", "immediate target must explicitly imply both B0 and B1")

    for index, source in enumerate(sources):
        sha = source.get("sha256")
        if sha is not None and (not isinstance(sha, str) or not SHA256_RE.fullmatch(sha)):
            _error(findings, f"sources[{index}].sha256", "must be a 64-character hexadecimal SHA-256")

    for index, counterexample in enumerate(counterexamples):
        location = f"counterexamples[{index}]"
        status = counterexample.get("status")
        verdict = counterexample.get("audit_verdict")
        if status not in enum_fields["status"]:
            _error(findings, f"{location}.status", f"value is not declared in status_model: {status}")
        if verdict not in enum_fields["audit_verdict"]:
            _error(
                findings,
                f"{location}.audit_verdict",
                f"value is not declared in status_model: {verdict}",
            )
        record_kind = counterexample.get("record_kind")
        if record_kind not in declared_counterexample_kinds:
            _error(
                findings,
                f"{location}.record_kind",
                "value is not declared in status_model.counterexample_record_kinds: "
                f"{record_kind}",
            )
        _nonempty_string(
            counterexample.get("exact_scope"), f"{location}.exact_scope", findings
        )
        _nonempty_string(counterexample.get("basis"), f"{location}.basis", findings)
        route_effect = counterexample.get("route_effect")
        if route_effect not in declared_route_effects:
            _error(
                findings,
                f"{location}.route_effect",
                "value is not declared in status_model.route_effects: "
                f"{route_effect}",
            )
        does_not_exclude = _string_list(
            counterexample.get("does_not_exclude"),
            f"{location}.does_not_exclude",
            findings,
        )
        if not does_not_exclude:
            _error(
                findings,
                f"{location}.does_not_exclude",
                "must state at least one non-inference boundary",
            )
        revival_trigger = counterexample.get("revival_trigger")
        if "revival_trigger" not in counterexample:
            _error(
                findings,
                f"{location}.revival_trigger",
                "is required and must be null or a non-empty string",
            )
        if revival_trigger is not None and (
            not isinstance(revival_trigger, str) or not revival_trigger.strip()
        ):
            _error(
                findings,
                f"{location}.revival_trigger",
                "must be null or a non-empty string",
            )
        evidence = _string_list(
            counterexample.get("evidence_refs"),
            f"{location}.evidence_refs",
            findings,
        )
        _check_refs(
            evidence,
            evidence_ids,
            f"{location}.evidence_refs",
            findings,
        )
        counterexample_id = str(counterexample.get("id") or "")
        if counterexample_id in evidence:
            _error(
                findings,
                f"{location}.evidence_refs",
                "a counterexample cannot cite itself as proof evidence",
            )
        if status == "active":
            if verdict not in {"verified", "verified_with_conditions"}:
                _error(
                    findings,
                    f"{location}.audit_verdict",
                    "active gates must be verified or verified_with_conditions",
                )
            if record_kind not in {"exact_counterexample", "mechanism_exclusion"}:
                _error(
                    findings,
                    f"{location}.record_kind",
                    "active gates must be exact counterexamples or mechanism exclusions",
                )
            if route_effect != "exclude_within_scope":
                _error(
                    findings,
                    f"{location}.route_effect",
                    "active gates must exclude only within their exact scope",
                )
            if not evidence:
                _error(
                    findings,
                    f"{location}.evidence_refs",
                    "active gates require proof-carrying evidence",
                )
            for ref in evidence:
                if ref in evidence_ids and ref not in active_gate_proof_evidence_ids:
                    _error(
                        findings,
                        f"{location}.evidence_refs",
                        f"active-gate evidence is not proof-carrying: {ref}",
                    )
            if evidence and not set(evidence) & active_gate_proof_anchor_ids:
                _error(
                    findings,
                    f"{location}.evidence_refs",
                    "active gates require an independently audited mathematical evidence anchor",
                )
        elif route_effect != "no_inference":
            _error(
                findings,
                f"{location}.route_effect",
                "nonactive counterexample records must have no_inference route effect",
            )
        if status == "paused" and (
            not isinstance(revival_trigger, str) or not revival_trigger.strip()
        ):
            _error(
                findings,
                f"{location}.revival_trigger",
                "paused records require a non-empty revival trigger",
            )

    for index, receipt in enumerate(receipts):
        location = f"audit_receipts[{index}]"
        receipt_kind = receipt.get("receipt_kind")
        if receipt_kind not in declared_receipt_kinds:
            _error(
                findings,
                f"{location}.receipt_kind",
                "value is not declared in status_model.receipt_kinds: "
                f"{receipt_kind}",
            )
        source_refs = _string_list(receipt.get("source_refs"), f"{location}.source_refs", findings)
        claim_refs = _string_list(receipt.get("claim_refs"), f"{location}.claim_refs", findings)
        evidence_refs = _string_list(receipt.get("evidence_refs"), f"{location}.evidence_refs", findings)
        _check_refs(source_refs, source_ids, f"{location}.source_refs", findings)
        _check_refs(claim_refs, claim_ids, f"{location}.claim_refs", findings)
        _check_refs(evidence_refs, evidence_ids, f"{location}.evidence_refs", findings)
        dispositions = receipt.get("route_dispositions")
        if receipt_kind == "campaign_closeout":
            _nonempty_string(
                receipt.get("campaign_id"), f"{location}.campaign_id", findings
            )
            if not isinstance(dispositions, list) or not dispositions:
                _error(
                    findings,
                    f"{location}.route_dispositions",
                    "campaign closeout requires at least one route disposition",
                )
                dispositions = []
        elif dispositions is None:
            dispositions = []
        elif not isinstance(dispositions, list):
            _error(
                findings, f"{location}.route_dispositions", "must be an array"
            )
            dispositions = []
        for disposition_index, disposition in enumerate(dispositions):
            _validate_route_disposition(
                disposition,
                location=(
                    f"{location}.route_dispositions[{disposition_index}]"
                ),
                allowed_decisions=declared_route_decisions,
                evidence_ids=evidence_ids,
                route_evidence_ids=route_evidence_ids,
                exclusion_gate_ids=exclusion_gate_ids,
                receipt_id=str(receipt.get("id") or ""),
                findings=findings,
            )

    return ValidationResult(
        findings=tuple(findings),
        counts={
            "claims": len(claims),
            "counterexamples": len(counterexamples),
            "dag_nodes": len(nodes),
            "dag_edges": len(edges),
            "sources": len(sources),
            "receipts": len(receipts),
        },
    )


def validate_file(path: Path) -> ValidationResult:
    """Load and validate a JSON research-state file."""

    try:
        value = loads_strict_json_bytes(path.read_bytes())
    except FileNotFoundError:
        return ValidationResult(
            findings=(Finding("ERROR", str(path), "research state file does not exist"),),
            counts={},
        )
    except (
        OSError,
        UnicodeDecodeError,
        ValueError,
        DuplicateKeyError,
        NonFiniteJSONError,
    ) as exc:
        return ValidationResult(
            findings=(Finding("ERROR", str(path), f"unable to load JSON: {exc}"),),
            counts={},
        )
    if not isinstance(value, Mapping):
        return ValidationResult(
            findings=(Finding("ERROR", str(path), "top-level JSON must be an object"),),
            counts={},
        )
    return validate_state(value)
