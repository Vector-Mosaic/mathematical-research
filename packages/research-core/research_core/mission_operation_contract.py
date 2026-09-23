"""Canonical compact contract for the RH Mission semantic console.

The nine operations below are the only executive-facing Mission capabilities.
Model and CLI surfaces project this owner; they may omit
operations but cannot redefine a branch.  Requests carry mathematical meaning
only.  MissionInterface injects all identity, authority, state, clock, byte,
storage, lease, and resource facts before composing domain owners.
"""

from __future__ import annotations

from typing import Any, Iterable, Mapping, Sequence

from .context_revision import (
    _validate_scientific_treatment,
    context_reference_key,
    validate_context_source_selection,
)
from .json_support import loads_strict_json_object
from .research_model import deep_freeze, deep_thaw
from .validator import _schema_violations


MISSION_OPERATION_CONTRACT_SCHEMA_VERSION = (
    "mathematical_research.mission_operation_contract.v1"
)
MISSION_SEMANTIC_REQUEST_SCHEMA_VERSION = (
    "mathematical_research.mission_semantic_request.v1"
)
MISSION_SEMANTIC_RESULT_SCHEMA_VERSION = (
    "mathematical_research.mission_semantic_result.v1"
)
MISSION_MODEL_PROJECTION_SCHEMA_VERSION = (
    "mathematical_research.mission_model_projection.v2"
)
MISSION_MODEL_USAGE_SCHEMA_VERSION = "mathematical_research.mission_model_usage.v1"
MISSION_CLI_PROJECTION_SCHEMA_VERSION = (
    "mathematical_research.mission_cli_projection.v1"
)
MISSION_HOST_MODEL_PROJECTION_SCHEMA_VERSION = (
    "mathematical_research.mission_host_model_projection.v1"
)

MISSION_INTERFACE_EXECUTE_METHOD = "execute_semantic_operation"
MISSION_INTERFACE_PREVIEW_METHOD = "preview_semantic_operation"

ORIENT = "orient"
RETRIEVE = "retrieve"
RECORD_CONTEXT = "record_context"
INTERPRET_MATERIAL = "interpret_material"
RECORD_CANDIDATE = "record_candidate"
RECORD_BRANCH = "record_branch"
SYNTHESIZE = "synthesize"
RECORD_STRATEGY = "record_strategy"
CHECKPOINT = "checkpoint"
MISSION_OPERATION_ORDER = (
    ORIENT,
    RETRIEVE,
    RECORD_CONTEXT,
    INTERPRET_MATERIAL,
    RECORD_CANDIDATE,
    RECORD_BRANCH,
    SYNTHESIZE,
    RECORD_STRATEGY,
    CHECKPOINT,
)
MISSION_MODEL_USAGE_OPERATION = "usage"
HISTORICAL_READ_OPERATIONS = (ORIENT, RETRIEVE)
HISTORICAL_SOURCE_FAMILIES = (
    "branches",
    "candidates",
    "strategies",
    "contexts",
    "evidence",
    "capture_annotations",
    "captures",
    "capture_artifacts",
)
HISTORICAL_ASSIGNMENT_MODES = (
    "historical_opportunity_scout",
    "lifecycle_historian",
)
HISTORICAL_RAW_BODY_POLICIES = (
    "metadata_only",
    "allow_untrusted_material",
)
HISTORICAL_RETRIEVE_MODES = (
    "history_inventory",
    "history_search",
    "history_read",
    "history_traverse",
)
HISTORICAL_SEARCH_FIELDS = ("id", "title", "content", "relationships")
HISTORICAL_RELATIONSHIP_KINDS = (
    "revision_predecessor",
    "owner_reference",
    "branch_genealogy",
    "candidate_genealogy",
    "strategy_hook",
    "capture_annotation",
    "capture_artifact",
    "capture_lineage",
)
RESEARCH_READ_MODES = ("usage", "retrieve")
RESEARCH_SOURCE_FAMILIES = (*HISTORICAL_SOURCE_FAMILIES, "missions", "sessions")
RESEARCH_READ_USAGE_TOPICS = ("reads", "sources", "continuations", "authority")
CANDIDATE_A1_REVIEW_MODES = ("usage", "retrieve", "submit")
CANDIDATE_A1_REVIEW_DISPOSITIONS = ("invalidated", "admission_ready")
ADMISSION_REVIEW_MODES = ("usage", "retrieve", "submit")
ADMISSION_DECISION_MODES = ("usage", "retrieve", "submit")
ADMISSION_REVIEW_DISPOSITIONS = ("no_material_objection", "material_objection")
ADMISSION_DECISION_DISPOSITIONS = ("authorize_exact_delta", "reject")
ADMISSION_HOST_GRANT_ROLES = ("reviewer", "admitter")


class MissionOperationContractError(ValueError):
    """Closed contract failure with one stable property-local code."""

    def __init__(self, code: str, location: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.location = location
        self.message = message


def _text() -> dict[str, Any]:
    return {"type": "string", "minLength": 1}


def _array(
    item: Mapping[str, Any],
    *,
    nonempty: bool = False,
    minimum: int | None = None,
) -> dict[str, Any]:
    return {
        "type": "array",
        "items": deep_thaw(item),
        "minItems": minimum if minimum is not None else (1 if nonempty else 0),
        "uniqueItems": True,
    }


def _closed(
    properties: Mapping[str, Mapping[str, Any]],
    required: Sequence[str] | None = None,
) -> dict[str, Any]:
    return {
        "type": "object",
        "additionalProperties": False,
        "properties": {key: deep_thaw(value) for key, value in properties.items()},
        "required": list(properties if required is None else required),
    }


def _enum(*values: str) -> dict[str, Any]:
    return {"type": "string", "enum": list(values)}


def _nullable(schema: Mapping[str, Any]) -> dict[str, Any]:
    return {"oneOf": [deep_thaw(schema), {"type": "null"}]}


_TEXT = _text()
_TEXTS = _array(_TEXT)
_LOWER_SHA256 = {
    "type": "string",
    "pattern": r"^[0-9a-f]{64}(?![\s\S])",
}


def _prefixed_record_id(
    *kinds: str,
    description: str,
    examples: Sequence[str],
) -> dict[str, Any]:
    alternatives = "|".join(kinds)
    return {
        "type": "string",
        "pattern": (
            rf"^(?:{alternatives}):"
            r"[A-Za-z0-9][A-Za-z0-9._:-]{0,191}(?![\s\S])"
        ),
        "description": description,
        "examples": list(examples),
    }


_CURRENT_OWNER_RECORD_ID = _prefixed_record_id(
    "mission",
    "strategy",
    "branch",
    "candidate",
    "context",
    "evidence",
    description=(
        "One exact unsuffixed current direct-owner id in <kind>:<identity> form. "
        "Select its identity from the current Executive orientation, a successful "
        "owner-write result, or bounded retrieve discovery; the selected owner "
        "must still be current. Do not put an id@revision handle in this id field."
    ),
    examples=("evidence:current-obstruction", "strategy:mission-wide"),
)
_OWNER_SELECTOR_RECORD_ID = _prefixed_record_id(
    "mission",
    "strategy",
    "branch",
    "candidate",
    "context",
    "evidence",
    description=(
        "One direct-owner id in <kind>:<identity> form. Select its identity from "
        "the current Executive orientation, a successful owner-write result, or "
        "bounded retrieve discovery. Keep id unsuffixed; an adjacent positive revision selects that exact "
        "meaning and is dependency-bound when still current, otherwise historical. "
        "Do not copy id@revision from retrieve into id."
    ),
    examples=("evidence:current-obstruction", "candidate:normalized-obstruction"),
)
_INTERPRETED_OWNER_SELECTOR_RECORD_ID = _prefixed_record_id(
    "evidence",
    "context",
    "branch",
    "candidate",
    description=(
        "One interpreted-owner id selected from the current Executive orientation, "
        "a successful owner-write result, or bounded retrieve discovery. Keep id unsuffixed and use "
        "the adjacent revision field for exact meaning; it is dependency-bound "
        "when still current and historical otherwise."
    ),
    examples=("evidence:current-obstruction", "candidate:normalized-obstruction"),
)
_SYNTHESIS_INPUT_RECORD_ID = _prefixed_record_id(
    "evidence",
    "candidate",
    description=(
        "One Evidence or Candidate id selected from the current Executive orientation, "
        "a successful owner-write result, or bounded retrieve discovery. Keep id unsuffixed and "
        "use the adjacent revision field for exact meaning; it is dependency-bound "
        "when still current and historical otherwise."
    ),
    examples=("evidence:first-obstruction", "candidate:normalized-obstruction"),
)
_CONTEXT_RECORD_ID = _prefixed_record_id(
    "context",
    description=(
        "The Context owner target id in context:<identity> form. Use the same "
        "id to revise an existing Context; do not append a retrieval revision."
    ),
    examples=("context:shared-research-question",),
)
_CANDIDATE_RECORD_ID = _prefixed_record_id(
    "candidate",
    description=(
        "The Candidate owner target id in candidate:<identity> form. Use the "
        "same id to revise an existing Candidate."
    ),
    examples=("candidate:normalized-obstruction",),
)
_BRANCH_RECORD_ID = _prefixed_record_id(
    "branch",
    description=(
        "The Discovery Branch owner target id in branch:<identity> form. Use "
        "the same id to revise an existing Branch."
    ),
    examples=("branch:normalized-obstruction",),
)
_EVIDENCE_RECORD_ID = _prefixed_record_id(
    "evidence",
    description=(
        "The Evidence owner id in evidence:<identity> form using the Store's "
        "safe 1-192 character identity grammar."
    ),
    examples=("evidence:captured-worker-result",),
)
_CAPTURE_RECORD_ID = {
    "type": "string",
    "pattern": r"^capture:raw-capture:[0-9a-f]{48}(?![\s\S])",
    "description": (
        "One immutable Raw Capture id copied exactly from authorized retrieve "
        "search or captures descriptors in capture:<capture_id> form."
    ),
    "examples": ["capture:raw-capture:" + ("a" * 48)],
}
_CAPTURE_ANNOTATION_RECORD_ID = _prefixed_record_id(
    "capture-annotation",
    description=(
        "The capture-scope annotation id in capture-annotation:<identity> form."
    ),
    examples=("capture-annotation:no-current-delta",),
)
_RETRIEVAL_RECORD_ID = {
    "type": "string",
    "pattern": (
        r"^(?:(?:candidate|branch|strategy|context|evidence|capture-annotation):"
        r"[A-Za-z0-9][A-Za-z0-9._:-]{0,191}(?:@[1-9][0-9]*)?"
        r"|capture:raw-capture:[0-9a-f]{48}"
        r"|capture-artifact:raw-capture:[0-9a-f]{48}#(?:0|[1-9][0-9]*))"
        r"(?![\s\S])"
    ),
    "description": (
        "Use one exact readable handle selected from the current Executive "
        "orientation, a successful owner-write result, or authorized bounded "
        "retrieve discovery. "
        "Versioned semantic-owner handles may end in @revision; immutable Raw "
        "Capture artifacts use capture-artifact:<capture_id>#<ordinal>."
    ),
    "examples": [
        "evidence:current-obstruction@1",
        "capture-artifact:raw-capture:" + ("a" * 48) + "#0",
    ],
}
_RETRIEVAL_SEARCH_QUERY = {
    "type": "string",
    "minLength": 1,
    "description": (
        "Free nonempty substring search text. To resolve a returned delegated "
        "result, copy its exact child-thread id without adding a prefix. Read "
        "handles belong in retrieve.read.ids instead."
    ),
    "examples": ["01a00000-0000-7000-8000-000000000001", "saddle normalization"],
}
_HISTORICAL_SOURCE_FAMILY = _enum(*HISTORICAL_SOURCE_FAMILIES)
_HISTORICAL_SOURCE_FAMILIES = _array(_HISTORICAL_SOURCE_FAMILY, nonempty=True)
_HISTORICAL_PAGE_SIZE = {"type": "integer", "minimum": 1, "maximum": 100}
_HISTORICAL_CURSOR = _TEXT
_HISTORICAL_SEARCH_FIELD = _enum(*HISTORICAL_SEARCH_FIELDS)
_HISTORICAL_RELATIONSHIP_KIND = _enum(*HISTORICAL_RELATIONSHIP_KINDS)


def _existing_owner_id(
    kind: str, *, label: str, example: str
) -> dict[str, Any]:
    return _prefixed_record_id(
        kind,
        description=(
            f"Use one existing {label} id in {kind}:<identity> form, selected from "
            "the current Executive orientation, a successful owner-write result, "
            "or bounded retrieve discovery. Keep id unsuffixed; an adjacent revision selects exact meaning "
            "and is dependency-bound when still current, otherwise historical."
        ),
        examples=(example,),
    )


_EXISTING_CONTEXT_RECORD_ID = _existing_owner_id(
    "context", label="Context", example="context:shared-research-question"
)
_EXISTING_CANDIDATE_RECORD_ID = _existing_owner_id(
    "candidate", label="Candidate", example="candidate:normalized-obstruction"
)
_EXISTING_BRANCH_RECORD_ID = _existing_owner_id(
    "branch", label="Discovery Branch", example="branch:normalized-obstruction"
)


def _selector(id_schema: Mapping[str, Any]) -> dict[str, Any]:
    return _closed(
        {
            "id": id_schema,
            "revision": {"type": "integer", "minimum": 1},
        },
        required=("id",),
    )


def _synthesis_input_selector(id_schema: Mapping[str, Any]) -> dict[str, Any]:
    return _closed(
        {
            "id": id_schema,
            "role": _TEXT,
            "revision": {"type": "integer", "minimum": 1},
        },
        required=("id", "role"),
    )


_OWNER_SELECTOR = _selector(_OWNER_SELECTOR_RECORD_ID)
_INTERPRETED_OWNER_SELECTOR = _selector(_INTERPRETED_OWNER_SELECTOR_RECORD_ID)
_SYNTHESIS_INPUT_SELECTOR = _synthesis_input_selector(_SYNTHESIS_INPUT_RECORD_ID)
_CANDIDATE_SELECTOR = _selector(_EXISTING_CANDIDATE_RECORD_ID)
_BRANCH_SELECTOR = _selector(_EXISTING_BRANCH_RECORD_ID)
_CONTEXT_SELECTOR = _selector(_EXISTING_CONTEXT_RECORD_ID)
_CONTEXT_SOURCE_SELECTION = {"oneOf": [
    _closed({"mode": {"const": "treatments"},
             "treatment_ids": _array(_TEXT, nonempty=True)}),
    _closed({"mode": {"const": "whole_context"}}),
]}
_CANDIDATE_BASIS_SELECTOR = _closed({
    **deep_thaw(_INTERPRETED_OWNER_SELECTOR["properties"]),
    "selection": _CONTEXT_SOURCE_SELECTION,
}, required=("id",))
_EXACT_OWNER_REFERENCE = _closed({
    "kind": _enum("mission", "strategy", "branch", "candidate", "context",
                  "evidence", "session", "capture-annotation"),
    "identity": _TEXT, "revision": {"type": "integer", "minimum": 1},
    "payload_sha256": _LOWER_SHA256,
})
_EXACT_CONTEXT_REFERENCE = _closed({
    **deep_thaw(_EXACT_OWNER_REFERENCE["properties"]), "kind": {"const": "context"},
})
_SCIENTIFIC_EXPOSURE = _closed({"context_id": _TEXT, "treatment_id": _TEXT})
_SCIENTIFIC_SOURCE = _closed({
    "reference": _EXACT_OWNER_REFERENCE,
    "selection": _nullable(_CONTEXT_SOURCE_SELECTION),
    "roles": _array(_enum("history", "recognition", "reliance"), nonempty=True),
    "why": _TEXT,
    "dependency": _nullable(_closed({
        "observed_current_reference": _EXACT_OWNER_REFERENCE,
        "change_that_matters": _TEXT, "dependent_judgment": _TEXT,
    })),
})


def _scientific_treatment_schema(*, paired: bool = False) -> dict[str, Any]:
    source = deep_thaw(_SCIENTIFIC_SOURCE)
    if paired:
        source["properties"]["reference"] = {"oneOf": [
            _EXACT_OWNER_REFERENCE, _closed({"source": {"const": "this_evidence"}}),
        ]}
    return _closed({"question": _TEXT, "account": _TEXT,
                    "qualifications": {"type": "array", "items": _TEXT},
                    "sources": _array(source)})


_SCIENTIFIC_TREATMENT = _scientific_treatment_schema()
_SCIENTIFIC_METADATA = {
    "purpose": _TEXT, "question": _TEXT, "known_omissions": _TEXTS,
    "restricted_uses": _TEXTS, "restrictions": _TEXTS,
    # This is the existing Context-owned representation, not a second
    # independence schema. The owner validates its nonempty JSON object.
    "independence_treatment": {"type": "object", "minProperties": 1},
}


def _scientific_updates_schema(*, paired: bool = False) -> dict[str, Any]:
    treatments = {"type": "object", "propertyNames": _TEXT,
                  "additionalProperties": _scientific_treatment_schema(paired=paired)}
    document = _closed({
        "schema_version": {"const": 3}, "kind": {"const": "first_class_context"},
        "project_id": _TEXT, "mission_id": _TEXT, "context_id": _TEXT,
        **_SCIENTIFIC_METADATA, "treatments": treatments,
        "exposed_treatments": _array(_SCIENTIFIC_EXPOSURE),
    })
    patch = _closed({
        "insert": treatments, "replace": treatments,
        "remove": _array(_closed({"treatment_id": _TEXT, "reason": _TEXT})),
        "exposure_add": _array(_SCIENTIFIC_EXPOSURE),
        "exposure_remove": _array(_closed({
            **_SCIENTIFIC_EXPOSURE["properties"], "reason": _TEXT,
        })),
        "metadata_replace": _closed(_SCIENTIFIC_METADATA, required=()),
    })
    return _array({"oneOf": [
        _closed({"context_id": _CONTEXT_RECORD_ID, "expected_head": {"type": "null"},
                 "create": document, "patch": {"type": "null"}}),
        _closed({"context_id": _CONTEXT_RECORD_ID, "expected_head": _EXACT_CONTEXT_REFERENCE,
                 "create": {"type": "null"}, "patch": patch}),
    ]}, nonempty=True)


_SCIENTIFIC_CONTEXT_INPUT = _closed({
    "mode": {"const": "scientific"}, "updates": _scientific_updates_schema(),
    "exposure_required": _array(_SCIENTIFIC_EXPOSURE),
})
_SCIENTIFIC_CONTINUATION = _closed({
    "updates": _scientific_updates_schema(paired=True),
    "exposure_required": _array(_SCIENTIFIC_EXPOSURE, nonempty=True),
})
_REF = _closed({"id": _CURRENT_OWNER_RECORD_ID, "why": _TEXT})
_DEPENDENCY = _closed(
    {
        "id": _CURRENT_OWNER_RECORD_ID,
        "change_that_matters": _TEXT,
        "dependent_judgment": _TEXT,
    }
)
_INDEPENDENCE = _closed(
    {"purpose": _TEXT, "temporarily_withheld": _TEXTS, "collision_condition": _TEXT}
)
_EXISTING_CAPTURE_SCOPE = _closed(
    {
        "captured_material_id": _CAPTURE_RECORD_ID,
        "artifact_ordinal": {"type": "integer", "minimum": 0},
        "exact_scope": _TEXT,
        "coverage": _enum("partial_artifact", "complete_artifact"),
    }
)
_ADOPTED_NATIVE_LINEAGE = _closed(
    {
        "material_kind": _enum("assignment", "output"),
        "parent_thread_id": _TEXT,
        "child_thread_id": _TEXT,
    }
)
_ADOPTED_ROOT_CAPTURE_SCOPE = _closed(
    {
        "adopted_root_material": _closed(
            {
                "channel": _TEXT,
                "content": _TEXT,
                "native_lineage": _ADOPTED_NATIVE_LINEAGE,
            },
            required=("channel", "content"),
        ),
        "exact_scope": _TEXT,
        "coverage": _enum("partial_artifact", "complete_artifact"),
    }
)
_CAPTURE_SCOPE = {
    "oneOf": [
        _EXISTING_CAPTURE_SCOPE,
        _ADOPTED_ROOT_CAPTURE_SCOPE,
    ]
}
_CAPTURE_ANNOTATION_INPUT = _closed(
    {
        "judgment": {"const": "reviewed_no_current_semantic_delta"},
        "annotation_id": _CAPTURE_ANNOTATION_RECORD_ID,
        "captured_material_id": _CAPTURE_RECORD_ID,
        "artifact_ordinal": {"type": "integer", "minimum": 0},
        "exact_scope": _TEXT,
        "coverage": {"const": "complete_artifact"},
        "lifecycle": _enum("active", "removed"),
    }
)
_CANDIDATE_STANDING = _closed(
    {
        "status": _enum("open", "withdrawn", "failed", "refuted_at_scope"),
        "basis": _TEXT,
        "scope": _TEXT,
    },
    required=("status", "basis"),
)
_ARGUMENT_AUTHORITY = _closed(
    {
        "class": _enum(
            "candidate_only",
            "evidence_interpretation",
            "canonical_mathematics",
            "formal_verification",
        ),
        "refs": _array(_CANDIDATE_BASIS_SELECTOR),
    },
    required=("class",),
)
_ARGUMENT_EDGE = _closed(
    {
        "edge_id": _TEXT,
        "edge_kind": _enum("argument", "composition"),
        "premises": _array(_TEXT, nonempty=True),
        "conclusion": _TEXT,
        "authority": _ARGUMENT_AUTHORITY,
    }
)
_CANDIDATE_GENEALOGY = _closed(
    {
        "relation": _enum(
            "successor_of",
            "alternative_to",
            "recombines",
            "split_from",
            "repair_of",
            "derived_from",
        ),
        "candidate": _CANDIDATE_SELECTOR,
    }
)
_COMPLETE_TARGET_CLAIM = _closed(
    {
        "target": {"const": "riemann_hypothesis"},
        "disposition": _enum("proof", "disproof"),
    }
)
_CANDIDATE_INPUT = _closed(
    {
        "candidate_id": _CANDIDATE_RECORD_ID,
        "predecessor": _CANDIDATE_SELECTOR,
        "proposal_kind": _enum(
            "mathematical_statement",
            "lemma",
            "mechanism",
            "construction",
            "proof_architecture",
        ),
        "exact_statement": _TEXT,
        "standing": _CANDIDATE_STANDING,
        "mechanism": _TEXT,
        "scope_and_reach": _TEXT,
        "objects": _TEXTS,
        "hypotheses": _TEXTS,
        "domain": _TEXT,
        "quantifiers": _TEXTS,
        "normalization": _TEXTS,
        "argument_edges": _array(_ARGUMENT_EDGE),
        "supporting_refs": _array(_CANDIDATE_BASIS_SELECTOR),
        "obligations": _TEXTS,
        "gaps": _TEXTS,
        "objections": _TEXTS,
        "circularity_risks": _TEXTS,
        "falsifiers": _TEXTS,
        "discriminators": _TEXTS,
        "limitations": _TEXTS,
        "non_inferences": _TEXTS,
        "genealogy": _array(_CANDIDATE_GENEALOGY),
        "complete_target_claim": _COMPLETE_TARGET_CLAIM,
    },
    required=("candidate_id", "proposal_kind", "exact_statement", "standing"),
)
_BRANCH_RELATION = _closed(
    {
        "relationship": _enum(
            "split_from", "successor_of", "supersedes", "merge_of"
        ),
        "branch": _BRANCH_SELECTOR,
    }
)
_BRANCH_FAILURE = _closed(
    {"scope": _TEXT, "finding": _TEXT, "owner_refs": _array(_OWNER_SELECTOR)},
    required=("scope", "finding"),
)
_BRANCH_NON_EXCLUSION = _closed(
    {"scope": _TEXT, "statement": _TEXT, "owner_refs": _array(_OWNER_SELECTOR)},
    required=("scope", "statement"),
)
_BRANCH_RESIDUE = _closed(
    {"residue": _TEXT, "owner_refs": _array(_OWNER_SELECTOR)},
    required=("residue",),
)
_BRANCH_INTERFACE = _closed(
    {"interface": _TEXT, "owner_refs": _array(_OWNER_SELECTOR)},
    required=("interface",),
)
_BRANCH_REVIVAL = _closed(
    {"condition": _TEXT, "owner_refs": _array(_OWNER_SELECTOR)},
    required=("condition",),
)
_BRANCH_INPUT = _closed(
    {
        "branch_id": _BRANCH_RECORD_ID,
        "question": _TEXT,
        "leverage_fingerprint": _TEXT,
        "target_hook": _TEXT,
        "genealogy": _array(_BRANCH_RELATION),
        "scoped_failures": _array(_BRANCH_FAILURE),
        "non_exclusions": _array(_BRANCH_NON_EXCLUSION),
        "retained_residue": _array(_BRANCH_RESIDUE),
        "composition_interfaces": _array(_BRANCH_INTERFACE),
        "recombination_interfaces": _array(_BRANCH_INTERFACE),
        "revival_conditions": _array(_BRANCH_REVIVAL),
        "nonclaims": _TEXTS,
        "owner_refs": _array(_OWNER_SELECTOR),
    },
    required=("branch_id", "question", "leverage_fingerprint", "target_hook"),
)
_CAUSAL_INPUT = _closed(
    {
        "source": _OWNER_SELECTOR,
        "decision_consequence": _TEXT,
    },
    required=("source", "decision_consequence"),
)
_SERIOUS_ALTERNATIVE = _closed(
    {
        "relationship": _enum("alternative", "complement"),
        "opportunity": _OWNER_SELECTOR,
        "qualitative_opportunity_cost": _TEXT,
    },
    required=(
        "relationship",
        "opportunity",
        "qualitative_opportunity_cost",
    ),
)
_FORMAL_REQUEST = _closed(
    {
        "purpose": _enum("targeted_falsification", "targeted_verification"),
        "context": _CONTEXT_SELECTOR,
    }
)
_SELECTED_BET = _closed(
    {
        "bet": _TEXT,
        "discriminator": _TEXT,
        "owner_refs": _array(_OWNER_SELECTOR),
        "formal_request": _FORMAL_REQUEST,
    },
    required=("bet", "discriminator"),
)
_BRANCH_ATTENTION = _closed(
    {
        "branch": _BRANCH_SELECTOR,
        "attention": _enum("active", "available", "dormant", "superseded"),
        "consequence": _TEXT,
    }
)
_CONTEXT_TREATMENT = _closed({"context": _CONTEXT_SELECTOR, "treatment": _TEXT})
_CREATIVITY_TREATMENT = _closed(
    {
        "treatment": _TEXT,
        "decision_consequence": _TEXT,
        "owner_refs": _array(_OWNER_SELECTOR),
    },
    required=("treatment", "decision_consequence"),
)
_STRATEGY_CONDITION = _closed(
    {"condition": _TEXT, "owner_refs": _array(_OWNER_SELECTOR)},
    required=("condition",),
)
_STRATEGY_INPUT = _closed(
    {
        "mission_continuation": {
            "type": "string",
            "enum": ["continue", "closeout"],
        },
        "integrated_comparison": _TEXT,
        "causal_inputs": _array(_CAUSAL_INPUT),
        "serious_opportunities": _array(_SERIOUS_ALTERNATIVE),
        "selected_bets": _array(_SELECTED_BET),
        "attention_actions": _array(_BRANCH_ATTENTION),
        "context_treatment": _array(_CONTEXT_TREATMENT),
        "creativity_treatment": _array(_CREATIVITY_TREATMENT),
        "reconsideration_conditions": _array(
            _STRATEGY_CONDITION, nonempty=True
        ),
        "reversal_conditions": _array(_STRATEGY_CONDITION),
        "revival_conditions": _array(_STRATEGY_CONDITION),
        "owner_refs": _array(_OWNER_SELECTOR),
    },
    required=(
        "mission_continuation",
        "integrated_comparison",
        "reconsideration_conditions",
    ),
)
_SYNTHESIS_INPUT = _SYNTHESIS_INPUT_SELECTOR
_EVIDENCE_CONSEQUENCE = _closed(
    {
        "kind": {"const": "evidence"},
        "evidence_id": _EVIDENCE_RECORD_ID,
        "statement": _TEXT,
        "scope": _TEXT,
        "strength": _TEXT,
        "semantic_role": _TEXT,
        "limitations": _TEXTS,
        "non_inferences": _array(_TEXT, nonempty=True),
        "decision_consequence": _TEXT,
        "expected_head": _nullable(_closed({
            **_EXACT_OWNER_REFERENCE["properties"], "kind": {"const": "evidence"},
        })),
        "scientific_continuation": _SCIENTIFIC_CONTINUATION,
    },
    required=(
        "kind",
        "evidence_id",
        "statement",
        "scope",
        "strength",
        "semantic_role",
        "non_inferences",
    ),
)
_CANDIDATE_CONSEQUENCE = _closed(
    {"kind": {"const": "candidate"}, "candidate": _CANDIDATE_INPUT}
)


def _locally_validated_consequence_schema(
    schema: Mapping[str, Any],
) -> dict[str, Any]:
    """Keep semantic ID failures local to one synthesis consequence.

    The projected descriptions and examples still teach every exact ID grammar,
    while MissionInterface resolves those IDs inside the independently
    correctable consequence boundary.  Enforcing the regex at the aggregate
    request boundary would make one malformed consequence suppress valid sibling
    writes, contradicting synthesis's accepted partial-result contract.
    """

    def relax(value: Any) -> Any:
        if isinstance(value, Mapping):
            result = {key: relax(item) for key, item in value.items() if key != "pattern"}
            if "pattern" in value and value.get("type") == "string":
                result.setdefault("minLength", 1)
            return result
        if isinstance(value, list):
            return [relax(item) for item in value]
        return value

    return relax(deep_thaw(schema))


_SYNTHESIS_CONSEQUENCE = {
    "oneOf": [
        _locally_validated_consequence_schema(_EVIDENCE_CONSEQUENCE),
        _locally_validated_consequence_schema(_CANDIDATE_CONSEQUENCE),
    ]
}
_LEGACY_RETRIEVE_INPUT_SCHEMA = {
    "oneOf": [
        _closed(
            {
                "mode": {"const": "search"},
                "purpose": _TEXT,
                "query": _RETRIEVAL_SEARCH_QUERY,
            }
        ),
        _closed(
            {
                "mode": {"const": "read"},
                "purpose": _TEXT,
                "ids": _array(_RETRIEVAL_RECORD_ID, nonempty=True),
            }
        ),
    ]
}
ROOT_RETRIEVE_MODES = (
    "read", "search", "inventory", "checkpoint", "changes_since_checkpoint",
    "hooks", "captures", "proof_attention", "publication_result",
)
ROOT_RETRIEVAL_KINDS = (
    "mission", "strategy", "branch", "candidate", "context", "evidence",
    "session", "capture", "capture-annotation",
)
ROOT_HOOK_CLASSES = ("unresolved", "revival", "recombination", "reconsideration", "reversal")
ROOT_CHECKPOINT_SECTIONS = (
    "summary", "transitive_owner_refs", "causal_pointers", "unresolved_pointers",
    "pending_capture_locators",
)
_ROOT_RETRIEVAL_RECORD_ID = {
    **deep_thaw(_RETRIEVAL_RECORD_ID),
    "pattern": _RETRIEVAL_RECORD_ID["pattern"].replace(
        "candidate|branch|strategy|context|evidence|capture-annotation",
        "mission|session|candidate|branch|strategy|context|evidence|capture-annotation",
    ),
}
_ROOT_PAGE_INPUT = {"page_size": _HISTORICAL_PAGE_SIZE, "cursor": _HISTORICAL_CURSOR}


def _root_query_schema(mode: str, properties: Mapping[str, Any], *, required: Sequence[str] = ()) -> dict[str, Any]:
    return _closed(
        {"mode": {"const": mode}, "purpose": _TEXT, **properties, **_ROOT_PAGE_INPUT},
        required=("mode", "purpose", *required),
    )


_ROOT_RETRIEVE_INPUT_SCHEMA = {
    "oneOf": [
        _closed({"mode": {"const": "publication_result"}, "command_id": _TEXT}),
        _closed({"mode": {"const": "read"}, "purpose": _TEXT,
                 "ids": _array(_ROOT_RETRIEVAL_RECORD_ID, nonempty=True)}),
        _root_query_schema("search", {
            "query": _RETRIEVAL_SEARCH_QUERY,
            "kinds": _array(_enum(*ROOT_RETRIEVAL_KINDS), nonempty=True),
            "fields": _array(_enum(*HISTORICAL_SEARCH_FIELDS), nonempty=True),
        }, required=("query",)),
        _root_query_schema("inventory", {
            "kinds": _array(_enum(*ROOT_RETRIEVAL_KINDS), nonempty=True),
        }),
        _root_query_schema("checkpoint", {
            "checkpoint_id": _TEXT, "section": _enum(*ROOT_CHECKPOINT_SECTIONS),
        }, required=("section",)),
        _root_query_schema("changes_since_checkpoint", {
            "checkpoint_id": _TEXT,
            "kinds": _array(_enum(*ROOT_RETRIEVAL_KINDS), nonempty=True),
            "relations": _array(_enum("new", "advanced", "retired"), nonempty=True),
        }),
        _root_query_schema("hooks", {
            "hook_classes": _array(_enum(*ROOT_HOOK_CLASSES), nonempty=True),
            "query": _RETRIEVAL_SEARCH_QUERY,
            "owner_kinds": _array(_enum(*ROOT_RETRIEVAL_KINDS), nonempty=True),
            "strategy_relation": _enum("any", "directly_referenced", "not_directly_referenced"),
        }),
        _root_query_schema("captures", {
            "query": _RETRIEVAL_SEARCH_QUERY,
            "view": _enum("all", "failed_interval_or_late_output"),
            "epoch_ids": _array(_TEXT, nonempty=True),
            "thread_ids": _array(_TEXT, nonempty=True),
            "capture_kinds": _array(_enum("assignment", "output"), nonempty=True),
            "cut_relation": _enum("any", "at_or_before_latest_checkpoint", "after_latest_checkpoint"),
            "origin_epoch_states": _array(_enum("unbound_to_epoch", "epoch_not_terminal", "checkpointed", "failed_before_checkpoint"), nonempty=True),
            "pending_state": _enum("any", "current_pending", "fully_covered"),
            "late_classifications": _array(_enum("unbound_to_epoch", "epoch_not_terminal", "at_or_before_terminal", "after_checkpoint_terminal", "after_failed_terminal"), nonempty=True),
        }),
        _root_query_schema("proof_attention", {}),
    ]
}
_HISTORICAL_RETRIEVE_INPUT_SCHEMA = {
    "oneOf": [
        *deep_thaw(_LEGACY_RETRIEVE_INPUT_SCHEMA["oneOf"]),
        _closed(
            {
                "mode": {"const": "history_inventory"},
                "purpose": _TEXT,
                "revision_scope": _enum("retained_history", "current_at_cut"),
                "source_families": _HISTORICAL_SOURCE_FAMILIES,
                "page_size": _HISTORICAL_PAGE_SIZE,
                "cursor": _HISTORICAL_CURSOR,
            },
            required=("mode", "purpose"),
        ),
        _closed(
            {
                "mode": {"const": "history_search"},
                "purpose": _TEXT,
                "revision_scope": _enum("retained_history", "current_at_cut"),
                "query": _RETRIEVAL_SEARCH_QUERY,
                "fields": _array(_HISTORICAL_SEARCH_FIELD, nonempty=True),
                "source_families": _HISTORICAL_SOURCE_FAMILIES,
                "page_size": _HISTORICAL_PAGE_SIZE,
                "cursor": _HISTORICAL_CURSOR,
            },
            required=("mode", "purpose", "query", "fields"),
        ),
        _closed(
            {
                "mode": {"const": "history_read"},
                "purpose": _TEXT,
                "ids": _array(_RETRIEVAL_RECORD_ID, nonempty=True),
                "include_raw_bodies": {"type": "boolean"},
            },
            required=("mode", "purpose", "ids", "include_raw_bodies"),
        ),
        _closed(
            {
                "mode": {"const": "history_traverse"},
                "purpose": _TEXT,
                "ids": _array(_RETRIEVAL_RECORD_ID, nonempty=True),
                "relationship_kinds": _array(
                    _HISTORICAL_RELATIONSHIP_KIND,
                    nonempty=True,
                ),
                "page_size": _HISTORICAL_PAGE_SIZE,
                "cursor": _HISTORICAL_CURSOR,
            },
            required=("mode", "purpose", "ids", "relationship_kinds"),
        ),
    ]
}
_INPUT_SCHEMAS: Mapping[str, Mapping[str, Any]] = {
    ORIENT: _closed({}),
    RETRIEVE: _ROOT_RETRIEVE_INPUT_SCHEMA,
    RECORD_CONTEXT: {"oneOf": [_SCIENTIFIC_CONTEXT_INPUT, _closed(
        {
            "context_id": _CONTEXT_RECORD_ID,
            "subject": _TEXT,
            "question": _TEXT,
            "material": _array(_REF, nonempty=True),
            "known_omissions": _TEXTS,
            "restricted_uses": _TEXTS,
            "restrictions": _TEXTS,
            "dependencies": _array(_DEPENDENCY),
            "independence": _nullable(_INDEPENDENCE),
            "historical_advisory": _closed(
                {
                    "assignment_mode": _enum(*HISTORICAL_ASSIGNMENT_MODES),
                    "search_lens": _TEXT,
                    "source_families": _HISTORICAL_SOURCE_FAMILIES,
                    "historical_references": _array(
                        _closed(
                            {
                                "id": _OWNER_SELECTOR_RECORD_ID,
                                "revision": {"type": "integer", "minimum": 1},
                                "why": _TEXT,
                            }
                        )
                    ),
                    "untrusted_material_locators": _array(
                        _closed(
                            {
                                "id": _RETRIEVAL_RECORD_ID,
                                "why": _TEXT,
                                "treatment": {
                                    "const": "untrusted_mathematical_material"
                                },
                            }
                        )
                    ),
                    "known_omissions": _TEXTS,
                    "coverage_limits": _TEXTS,
                }
            ),
        },
        required=("context_id", "subject", "question", "material"),
    )]},
    INTERPRET_MATERIAL: {
        "oneOf": [
            _closed(
                {
                    "evidence_id": _EVIDENCE_RECORD_ID,
                    "capture_scopes": _array(_CAPTURE_SCOPE, nonempty=True),
                    "interpretation": _TEXT,
                    "scope": _TEXT,
                    "strength": _enum(
                        "heuristic",
                        "numerical_diagnostic",
                        "exact_finite_identity",
                        "rigorous_bounded_result",
                        "conditional_theorem",
                        "general_theorem",
                        "formal_certificate",
                        "exact_counterexample",
                        "source",
                        "objection",
                    ),
                    "limitations": _TEXTS,
                    "significance": _TEXT,
                    "expected_head": _nullable(_closed({
                        **_EXACT_OWNER_REFERENCE["properties"], "kind": {"const": "evidence"},
                    })),
                    "scientific_continuation": _SCIENTIFIC_CONTINUATION,
                },
                required=("evidence_id", "capture_scopes", "interpretation", "scope",
                          "strength", "limitations", "significance"),
            ),
            _CAPTURE_ANNOTATION_INPUT,
        ]
    },
    RECORD_CANDIDATE: _CANDIDATE_INPUT,
    RECORD_BRANCH: _BRANCH_INPUT,
    SYNTHESIZE: _closed(
        {
            "relation_question": _TEXT,
            "inputs": _array(_SYNTHESIS_INPUT, minimum=2),
            "compatibility_analysis": _TEXT,
            "derivation_or_incompatibility": _TEXT,
            "scope": _TEXT,
            "strength": _TEXT,
            "dependencies": _array(_INTERPRETED_OWNER_SELECTOR),
            "limitations": _TEXTS,
            "non_inferences": _TEXTS,
            "edge_survival": _TEXT,
            "consequences": _array(_SYNTHESIS_CONSEQUENCE),
        },
        required=(
            "relation_question",
            "inputs",
            "compatibility_analysis",
            "derivation_or_incompatibility",
            "scope",
            "strength",
            "edge_survival",
        ),
    ),
    RECORD_STRATEGY: _STRATEGY_INPUT,
    CHECKPOINT: _closed({}),
}

_MODEL_INPUT_EXAMPLES: Mapping[str, Sequence[Mapping[str, Any]]] = {
    ORIENT: (
        {"label": "Read the current Executive orientation", "input": {}},
    ),
    RETRIEVE: (
        {
            "label": "Resolve one returned worker result by exact child thread id",
            "input": {
                "mode": "search",
                "purpose": "Resolve the automatically captured returned result.",
                "query": "01a00000-0000-7000-8000-000000000001",
            },
        },
        {
            "label": "Read one exact preserved capture artifact",
            "input": {
                "mode": "read",
                "purpose": "Read the exact captured result selected for interpretation.",
                "ids": ["capture-artifact:raw-capture:" + ("a" * 48) + "#0"],
            },
        },
        {"label": "Discover current owner handles without loading their bodies", "input": {"mode": "inventory", "purpose": "Select exact current owner material.", "page_size": 25}},
        {"label": "Read exact checkpoint closure selectors", "input": {"mode": "checkpoint", "purpose": "Select retained checkpoint ground.", "section": "transitive_owner_refs", "page_size": 25}},
        {"label": "Discover factual changes across owner kinds since checkpoint", "input": {"mode": "changes_since_checkpoint", "purpose": "Inspect exact changed identities across owner kinds.", "page_size": 25}},
        {"label": "Discover authored hooks outside current Strategy selection", "input": {"mode": "hooks", "purpose": "Inspect authored alternative research hooks.", "strategy_relation": "not_directly_referenced", "page_size": 25}},
        {"label": "Inspect retained failed-interval and late output descriptors", "input": {"mode": "captures", "purpose": "Inspect retained output relevant to failed-epoch continuity.", "view": "failed_interval_or_late_output", "page_size": 25}},
        {"label": "Refresh exact OPEN A1 attention without specialist authority", "input": {"mode": "proof_attention", "purpose": "Select exact unverified complete-claim attention.", "page_size": 25}},
    ),
    RECORD_CONTEXT: (
        {
            "label": "Record one shared question-shaped Context using unsuffixed current-owner record ids",
            "input": {
                "context_id": "context:shared-research-question",
                "subject": "A shared mathematical question for two research branches.",
                "question": "Which exact obstruction survives the current normalization?",
                "material": [
                    {
                        "id": "evidence:current-obstruction",
                        "why": "It supplies the indispensable current obstruction.",
                    }
                ],
                "dependencies": [
                    {
                        "id": "strategy:mission-wide",
                        "change_that_matters": "A different surviving obstruction becomes primary.",
                        "dependent_judgment": "The Context question must then be reconsidered.",
                    }
                ],
            },
        },
    ),
    INTERPRET_MATERIAL: (
        {
            "label": "Interpret one existing captured worker result",
            "input": {
                "evidence_id": "evidence:captured-worker-result",
                "capture_scopes": [
                    {
                        "captured_material_id": "capture:raw-capture:" + ("a" * 48),
                        "artifact_ordinal": 0,
                        "exact_scope": "The complete returned worker result.",
                        "coverage": "complete_artifact",
                    }
                ],
                "interpretation": "The result proves only the stated bounded lemma.",
                "scope": "The exact hypotheses stated in the captured result.",
                "strength": "rigorous_bounded_result",
                "limitations": ["No asymptotic extension is established."],
                "significance": "It closes one current Strategy obligation.",
            },
        },
        {
            "label": "Adopt consequential root material into Evidence",
            "input": {
                "evidence_id": "evidence:adopted-root-material",
                "capture_scopes": [
                    {
                        "adopted_root_material": {
                            "channel": "web_search",
                            "content": "Exact selected source text or root computation output.",
                        },
                        "exact_scope": "The exact selected root material above.",
                        "coverage": "complete_artifact",
                    }
                ],
                "interpretation": "The source supports the stated definition only.",
                "scope": "The selected source passage.",
                "strength": "source",
                "limitations": ["No downstream theorem follows automatically."],
                "significance": "It fixes the definition used by the active branch.",
            },
        },
        {
            "label": "Retain a reviewed capture with no current semantic delta",
            "input": {
                "judgment": "reviewed_no_current_semantic_delta",
                "annotation_id": "capture-annotation:no-current-delta",
                "captured_material_id": "capture:raw-capture:" + ("a" * 48),
                "artifact_ordinal": 0,
                "exact_scope": "The complete captured artifact.",
                "coverage": "complete_artifact",
                "lifecycle": "active",
            },
        },
    ),
    RECORD_CANDIDATE: (
        {
            "label": "Record one ordinary unproved Candidate",
            "input": {
                "candidate_id": "candidate:normalized-obstruction",
                "proposal_kind": "lemma",
                "exact_statement": "The normalized obstruction is positive at n=3.",
                "standing": {
                    "status": "open",
                    "basis": "The exact finite calculation has not been generalized.",
                },
            },
        },
        {
            "label": "Preserve a purported complete RH proof as an unverified Candidate",
            "input": {
                "candidate_id": "candidate:purported-complete-rh-proof",
                "proposal_kind": "proof_architecture",
                "exact_statement": "This purported argument proves the Riemann Hypothesis.",
                "standing": {
                    "status": "open",
                    "basis": "The purported complete proof requires independent adversarial verification.",
                    "scope": "Unverified purported complete RH proof.",
                },
                "mechanism": "A purported unconditional argument for every nontrivial zero.",
                "scope_and_reach": "Complete proof of the Riemann Hypothesis.",
                "complete_target_claim": {
                    "target": "riemann_hypothesis",
                    "disposition": "proof",
                },
                "non_inferences": [
                    "Do not infer that RH is proved or that the Candidate has survived review."
                ],
            },
        },
    ),
    RECORD_BRANCH: (
        {
            "label": "Record one mathematical Discovery Branch",
            "input": {
                "branch_id": "branch:normalized-obstruction",
                "question": "Does the normalized obstruction persist?",
                "leverage_fingerprint": "A surviving sign defect rules out this route.",
                "target_hook": "candidate:normalized-obstruction",
            },
        },
    ),
    SYNTHESIZE: (
        {
            "label": "Relate two exact interpreted owner meanings",
            "input": {
                "relation_question": "Do the two exact obstructions compose?",
                "inputs": [
                    {"id": "evidence:first-obstruction", "role": "premise"},
                    {"id": "evidence:second-obstruction", "role": "comparison"},
                ],
                "compatibility_analysis": "They use the same normalization.",
                "derivation_or_incompatibility": "No stronger consequence is justified yet.",
                "scope": "The two selected Evidence revisions only.",
                "strength": "rigorous scoped comparison",
                "edge_survival": "The shared-normalization composition edge survives.",
            },
        },
    ),
    RECORD_STRATEGY: (
        {
            "label": "Record one causal Mission-wide Strategy judgment",
            "input": {
                "mission_continuation": "continue",
                "integrated_comparison": "The obstruction branch has the sharpest current discriminator.",
                "reconsideration_conditions": [
                    {
                        "condition": "Reconsider when the obstruction fails the normalized test."
                    }
                ],
            },
        },
    ),
    CHECKPOINT: (
        {"label": "End the current Executive Epoch", "input": {}},
    ),
}

_READABLE_ITEM = _closed(
    {
        "id": _TEXT,
        "kind": _TEXT,
        "title": _TEXT,
        "readable_content": _TEXT,
        "completeness": _enum("complete", "bounded_fragment", "structured_view", "descriptor"),
    }
)
_HISTORICAL_MATCH_PROVENANCE = _closed(
    {
        "field": _enum(*HISTORICAL_SEARCH_FIELDS),
        "query": _TEXT,
    }
)
_HISTORICAL_EDGE_PROVENANCE = _closed(
    {
        "relationship_kind": _enum(*HISTORICAL_RELATIONSHIP_KINDS),
        "source_id": _RETRIEVAL_RECORD_ID,
        "target_id": _RETRIEVAL_RECORD_ID,
        "source_path": _TEXT,
    }
)
_HISTORICAL_READABLE_ITEM = _closed(
    {
        "id": _TEXT,
        "kind": _TEXT,
        "title": _TEXT,
        "readable_content": _TEXT,
        "completeness": _enum("complete", "bounded_fragment", "structured_view", "descriptor"),
        "trust_class": _enum(
            "canonical_semantic_owner",
            "immutable_capture",
            "untrusted_mathematical_material",
            "structured_relationship",
        ),
        "match_provenance": _array(_HISTORICAL_MATCH_PROVENANCE),
        "edge_provenance": _array(_HISTORICAL_EDGE_PROVENANCE),
    }
)
_HISTORICAL_CURRENT_GROUND = _closed(
    {
        "id": _OWNER_SELECTOR_RECORD_ID,
        "revision": {"type": "integer", "minimum": 1},
        "why": _TEXT,
    }
)
_HISTORICAL_CURRENT_OWNER = _closed(
    {
        "id": _OWNER_SELECTOR_RECORD_ID,
        "revision": {"type": "integer", "minimum": 1},
        "retrieval": _TEXT,
        "provenance": _TEXT,
    }
)
_HISTORICAL_INVALIDATION = _closed(
    {
        "id": _OWNER_SELECTOR_RECORD_ID,
        "revision": {"type": "integer", "minimum": 1},
        "condition": _TEXT,
    }
)
_HISTORICAL_ORIENTATION = _closed(
    {
        "schema_version": {"const": "mathematical_research.historical_orientation.v2"},
        "mission_purpose": _TEXT,
        "proof_boundary": _TEXT,
        "context_id": _EXISTING_CONTEXT_RECORD_ID,
        "context_revision": {"type": "integer", "minimum": 1},
        "context_purpose": _TEXT,
        "question": _TEXT,
        "bottleneck_or_search_lens": _TEXT,
        "assignment_mode": _enum(*HISTORICAL_ASSIGNMENT_MODES),
        "assignment": _TEXT,
        "source_families": _HISTORICAL_SOURCE_FAMILIES,
        "indispensable_ground": _array(
            _HISTORICAL_CURRENT_GROUND,
            nonempty=True,
        ),
        "current_owner_references": _array(
            _HISTORICAL_CURRENT_OWNER,
            nonempty=True,
        ),
        "known_omissions": _TEXTS,
        "restricted_uses": _TEXTS,
        "restrictions": _TEXTS,
        "invalidation_conditions": _array(_HISTORICAL_INVALIDATION),
        "coverage_limits": _TEXTS,
        "assignment_ground": _array(_READABLE_ITEM),
        "retrieval_routes": _array(_TEXT, nonempty=True),
    }
)
_HISTORICAL_READ_RESULT = _closed(
    {
        "view": {"const": "historical"},
        "purpose": _TEXT,
        "mode": _enum("orient", *HISTORICAL_RETRIEVE_MODES),
        "project_commit_cut": {"type": "integer", "minimum": 0},
        "source_families": _HISTORICAL_SOURCE_FAMILIES,
        "raw_body_policy": _enum(*HISTORICAL_RAW_BODY_POLICIES),
        "coverage": _closed(
            {
                "meaning": _TEXT,
                "omissions": _TEXTS,
                "limits": _TEXTS,
            }
        ),
        "orientation": _nullable(_HISTORICAL_ORIENTATION),
        "items": _array(_HISTORICAL_READABLE_ITEM),
        "next_cursor": _nullable(_HISTORICAL_CURSOR),
    }
)
_READ_SELECTOR = _closed({"id": _TEXT, "revision": {"type": "integer", "minimum": 1}})
_STRATEGY_CONNECTION = _closed({"json_pointer": _TEXT, "role": _enum(
    "causal_input", "serious_opportunity", "selected_bet_ground", "branch_attention",
    "context_treatment", "creativity_ground", "reconsideration_ground", "reversal_ground",
    "revival_ground", "strategy_owner_ref", "formal_context",
)})
_RETRIEVE_CALL = _closed({"operation": {"const": "retrieve"}, "input": _ROOT_RETRIEVE_INPUT_SCHEMA})
_USAGE_CALL = _closed({"operation": {"const": "usage"}, "input": _closed({"for_operation": _enum("retrieve", "checkpoint")})})
_FORMAL_REQUEST = _closed({"selected_bet_sha256": _LOWER_SHA256, "bet": _TEXT,
    "discriminator": _TEXT, "purpose": _enum("targeted_falsification", "targeted_verification"), "context_retrieval_handle": _TEXT})
_FORMAL_TERMINAL = _closed({"attempt_id": _TEXT, "attempt_state": _TEXT, "raw_capture_handle": _nullable(_TEXT)})
_ADMISSION_READ_REF = _closed({"id": _TEXT, "revision": {"type": "integer", "minimum": 1}, "retrieval_handle": _TEXT})
_ADMITTED_RESULT = _closed({"target": {"const": "riemann_hypothesis"}, "disposition": _enum("proved", "disproved"),
    "theorem_or_counterexample_claim": _TEXT,
    "candidate_ref": _closed({"mission_id": _TEXT, "candidate_id": _TEXT, "revision": {"type": "integer", "minimum": 1}, "digest_sha256": _LOWER_SHA256}),
    "admission_decision_ref": _closed({"decision_id": _TEXT, "digest_sha256": _LOWER_SHA256})})
_OPEN_A1_ATTENTION = _closed({
    "candidate_ref": _closed({"id": _TEXT, "revision": {"type": "integer", "minimum": 1}, "payload_sha256": _LOWER_SHA256}),
    "retrieval_handle": _TEXT, "claim_disposition": _enum("proof", "disproof", "legacy_unspecified"),
    "stage": _enum("awaiting_a1_review", "awaiting_admission_case", "awaiting_admission_review", "awaiting_admission_decision", "awaiting_external_canonical_rebind"),
    "triage": _nullable(_closed({"disposition": _enum("admission_ready"), "evidence_ref": _READ_SELECTOR, "retrieval_handle": _TEXT})),
    "admission": _nullable(_closed({"case": _ADMISSION_READ_REF,
        "review": _nullable(_closed({**_ADMISSION_READ_REF["properties"], "disposition": _enum(*ADMISSION_REVIEW_DISPOSITIONS)})),
        "decision": _nullable(_closed({**_ADMISSION_READ_REF["properties"], "disposition": _enum("authorize_exact_delta")}))})),
})
_PROOF_ATTENTION = _closed({"open_candidate_a1": _array(_OPEN_A1_ATTENTION), "admitted_result": _nullable(_ADMITTED_RESULT)})
_CHECKPOINT_SUMMARY = _closed({"checkpoint_id": _TEXT, "mission_root_handle": _TEXT,
    "strategy_root_handle": _TEXT, "predecessor_checkpoint_id": _nullable(_TEXT)})
_COUNTS = _closed({key: {"type": "integer", "minimum": 0} for key in ("new", "advanced", "retired")})
_PURPOSE_READ = _closed({"objective": _TEXT, "proof_standard": _TEXT, "non_goals": _TEXTS,
    "closeout_conditions": _closed({"strategy_mission_continuation": _TEXT, "semantic_effect": _TEXT})})
_STRATEGY_READ_FIELDS = ("mission_continuation", "integrated_comparison", "causal_inputs", "serious_opportunities", "selected_bets", "attention_actions", "context_treatment", "creativity_treatment", "reconsideration_conditions", "reversal_conditions", "revival_conditions", "owner_refs")
# Source-change qualifications and exact read have different jobs. Each owner
# keeps its declared qualification fields; arbitrary exact-read fields do not
# expand this projection. Authored mathematics remains literal, not scrubbed.
_EVIDENCE_QUALIFICATION_SUBJECTS = {
    "evidence_meaning": _closed(
        {"statement": _TEXT, "semantic_role": _TEXT, "decision_consequence": _TEXT},
        required=("statement", "semantic_role"),
    ),
    "candidate_a1_triage": _closed({field: {} for field in (
        "candidate_ref", "disposition", "review_finding", "cited_basis",
        "concrete_defects", "no_remaining_material_objection", "limitations",
        "non_inferences",
    )}, required=()),
    "complete_claim_admission_case": _closed({field: {} for field in (
        "candidate_ref", "triage_ref", "candidate_disposition", "exact_claim",
        "source_closure",
    )}, required=()),
    "complete_claim_admission_review": _closed({field: {} for field in (
        "candidate_ref", "case_ref", "disposition", "review_finding",
        "objections", "cited_basis",
    )}, required=()),
    "complete_claim_admission_decision": _closed({field: {} for field in (
        "candidate_ref", "case_ref", "review_ref", "disposition",
        "decision_basis", "objections", "cited_basis",
    )}, required=()),
    "purported_complete_route": _closed({field: {} for field in (
        "kind", "candidate_id", "claimed_scope",
    )}, required=()),
}
_SOURCE_OWNER_QUALIFICATION_SCHEMAS = {
    "mission": _closed({field: {} for field in (
        "lifecycle", "effective", "autonomous", "purpose", "scientific_context_id",
    )}, required=()),
    "strategy": _closed({
        **{field: {} for field in _STRATEGY_READ_FIELDS},
        "formal_requests": _array(_FORMAL_REQUEST),
    }, required=()),
    "branch": _closed({field: {} for field in (
        "question", "leverage_fingerprint", "target_hook", "genealogy",
        "scoped_failures", "non_exclusions", "retained_residue",
        "composition_interfaces", "recombination_interfaces",
        "revival_conditions", "nonclaims", "owner_refs",
    )}, required=()),
    "candidate": _closed({field: {} for field in (
        "proposal_kind", "exact_statement", "standing", "mechanism",
        "scope_and_reach", "complete_target_claim", "objects", "hypotheses",
        "domain", "quantifiers", "normalization", "supporting_refs",
        "obligations", "gaps", "objections", "circularity_risks", "falsifiers",
        "discriminators", "limitations", "non_inferences", "genealogy",
        "lifecycle", "claimed_scope", "normalizations", "dependencies",
        "component_evidence_refs", "smallest_falsifier", "full_rh_case",
    )}, required=()),
    "session": _closed({field: {} for field in (
        "lifecycle", "mission_ref", "strategy_ref", "selected_bet_sha256",
        "selected_bet", "context_ref", "terminal_binding",
    )}, required=()),
    "capture-annotation": _closed({field: {} for field in (
        "annotation_id", "capture_id", "exact_scope", "lifecycle", "annotation_kind",
    )}, required=()),
}
_ORIENTATION_CONTENT_REF = _closed({"orientation_path": _TEXT})
_SCIENTIFIC_CONTEXT_VIEW = _closed({
    **_SCIENTIFIC_METADATA,
    "treatments": {"type": "object", "propertyNames": _TEXT,
                   "additionalProperties": _SCIENTIFIC_TREATMENT},
    "exposed_treatments": _array(_SCIENTIFIC_EXPOSURE),
})
_SCIENTIFIC_QUALIFICATION_METADATA = _closed({
    field: _SCIENTIFIC_METADATA[field]
    for field in ("known_omissions", "restricted_uses", "restrictions", "independence_treatment")
})
_LEGACY_CONTEXT_VIEW = _closed({field: {} for field in (
    "purpose", "question", "indispensable_ground", "owner_source_references",
    "known_omissions", "restricted_uses", "independence_treatment", "restrictions",
    "invalidation_conditions", "immutable_historical_references",
    "untrusted_material_locators", "historical_advisory_scope",
)}, required=("purpose", "question", "indispensable_ground", "owner_source_references",
              "known_omissions", "restricted_uses", "independence_treatment",
              "restrictions", "invalidation_conditions"))
_SOURCE_EVIDENCE_QUALIFICATION = {"oneOf": [
    _closed({"subtype": {"const": subtype}, "subject": (
        _closed({**subject["properties"], **{field: {} for field in (
            "objects", "hypotheses", "domain", "normalization", "quantifiers",
            "parent_bet_outcome", "standing",
        )}}, required=subject["required"])
        if subtype == "evidence_meaning" else subject),
        "exact_scope": {}, "rigor": {}, "limitations": {}, "non_inferences": {}})
    for subtype, subject in _EVIDENCE_QUALIFICATION_SUBJECTS.items()
]}
_SOURCE_QUALIFICATION = {"anyOf": [
    _closed({"selection": _closed({"mode": {"const": "treatments"},
                                    "treatment_ids": _array(_TEXT, nonempty=True)}),
             "treatments": {"type": "object", "propertyNames": _TEXT,
                            "additionalProperties": _SCIENTIFIC_TREATMENT},
             "context_qualifications": _SCIENTIFIC_QUALIFICATION_METADATA}),
    _closed({"selection": _closed({"mode": {"const": "whole_context"}}),
             "context": {"oneOf": [_SCIENTIFIC_CONTEXT_VIEW, _LEGACY_CONTEXT_VIEW]}}),
    # Non-Context projections retain their owning semantic qualification fields;
    # their kind and exact identity are supplied by current_reference.
    _SOURCE_EVIDENCE_QUALIFICATION,
    *_SOURCE_OWNER_QUALIFICATION_SCHEMAS.values(),
]}
_SCIENTIFIC_READ_CALL = _closed({
    "operation": {"const": "retrieve"},
    "input": _closed({"mode": {"const": "read"}, "purpose": _TEXT,
                      "ids": _array(_ROOT_RETRIEVAL_RECORD_ID, nonempty=True)}),
})
_SCIENTIFIC_UNAVAILABLE = {"oneOf": [
    _closed({"kind": {"const": "context"}, "context_id": _TEXT,
             "treatment_id": _nullable(_TEXT),
             "reason_code": _enum("not_found", "wrong_scope", "unsupported_version", "integrity_failure", "access_denied"),
             "read_call": _nullable(_SCIENTIFIC_READ_CALL)}),
    _closed({"kind": {"const": "source"}, "reference": _EXACT_OWNER_REFERENCE,
             "reason_code": _enum("not_found", "wrong_scope", "unsupported_version", "integrity_failure", "access_denied"),
             "read_call": _nullable(_SCIENTIFIC_READ_CALL)}),
]}
_SCIENTIFIC_CONTEXT = _closed({
    "state": _enum("unbound", "available", "partial", "unavailable"),
    "binding": _nullable(_closed({"context_id": _TEXT})),
    "root_reference": _nullable(_EXACT_CONTEXT_REFERENCE),
    "purpose": _nullable(_TEXT), "question": _nullable(_TEXT),
    "known_omissions": _TEXTS, "restricted_uses": _TEXTS, "restrictions": _TEXTS,
    "independence_treatment": _nullable(_SCIENTIFIC_METADATA["independence_treatment"]),
    "treatments": _array(_closed({
        "context_reference": _EXACT_CONTEXT_REFERENCE,
        "treatment_id": _TEXT,
        "content": _SCIENTIFIC_TREATMENT,
        "context_qualifications": _SCIENTIFIC_QUALIFICATION_METADATA,
    }, required=("context_reference", "treatment_id", "content"))),
    "source_changes": _array(_closed({
        "cited_reference": _EXACT_OWNER_REFERENCE,
        "current_reference": _nullable(_EXACT_OWNER_REFERENCE),
        "selection": _nullable(_CONTEXT_SOURCE_SELECTION),
        "state": _enum("advanced_same_identity", "no_current_head", "current_unavailable"),
        "affected": _array(_closed({"context_reference": _EXACT_CONTEXT_REFERENCE,
                                     "treatment_id": _TEXT,
                                     "roles": _array(_enum("history", "recognition", "reliance"), nonempty=True)}), nonempty=True),
        "qualification": _nullable(_SOURCE_QUALIFICATION),
        "qualification_ref": _nullable(_ORIENTATION_CONTENT_REF),
        "read_call": _nullable(_SCIENTIFIC_READ_CALL),
    })),
    "unavailable": _array(_SCIENTIFIC_UNAVAILABLE),
})
_EXECUTIVE_ORIENTATION = _closed({
    "schema_version": {"const": "mathematical_research.executive_orientation.v3"},
    "target": _closed({"target": {"const": "riemann_hypothesis"}, "statement": {"const": "Every nontrivial zero of the Riemann zeta function has real part one half."}, "canonical_status": _enum("open", "proved", "disproved")}),
    "mission": _closed({"handle": _TEXT, "lifecycle": _TEXT, "effective": {"type": "boolean"}, "autonomous": {"type": "boolean"}, "purpose": _PURPOSE_READ, "scientific_context_id": _nullable(_TEXT)}),
    "current_strategy": _closed({"handle": _TEXT, **{field: {} for field in _STRATEGY_READ_FIELDS}, "formal_requests": _array(_FORMAL_REQUEST)}),
    "scientific_context": _SCIENTIFIC_CONTEXT,
    "continuity": _closed({"checkpoint": _nullable(_CHECKPOINT_SUMMARY),
        "mission_relation_to_checkpoint": _enum("no_checkpoint", "same_revision", "advanced_same_identity", "different_lineage"),
        "strategy_relation_to_checkpoint": _enum("no_checkpoint", "same_revision", "advanced_same_identity", "different_lineage"),
        "mission_strategy_changes_since_checkpoint": _closed({"state": _enum("no_checkpoint", "none", "present"),
            "counts_by_kind": _closed({kind: _COUNTS for kind in ("mission", "strategy")}, required=()), "retrieve_call": _nullable(_RETRIEVE_CALL)}),
        "checkpoint_attention": _closed({"unresolved_pointer_count": {"type": "integer", "minimum": 0}, "retrieve_call": _nullable(_RETRIEVE_CALL)})}),
    "proof_attention": _PROOF_ATTENTION,
    "formal_attention": _array(_closed({**_FORMAL_REQUEST["properties"], "session_state": _enum("not_started", "open", "terminal"), "session_handle": _nullable(_TEXT), "terminal": _nullable(_FORMAL_TERMINAL)})),
    "retrieval": _closed({"usage_call": _USAGE_CALL, "available_modes": _array(_enum(*ROOT_RETRIEVE_MODES)), "recommended_calls": _array(_RETRIEVE_CALL)}),
})
_ROOT_READ_OUTCOME = {"oneOf": [
    _closed({"requested_id": _TEXT, "status": {"const": "readable"}, "id": _TEXT, "kind": _TEXT, "title": _TEXT,
        "readable_content": {"type": "string"}, "completeness": _enum("complete", "structured_view", "complete_declared_semantic_projection"),
        "trust_class": _enum("semantic_owner_projection", "untrusted_raw_material", "custody_descriptor")}),
    _closed({"requested_id": _TEXT, "status": _enum("not_found", "unavailable"), "reason": _TEXT, "correction": _TEXT}),
]}
_DESCRIPTOR = _closed({"id": _TEXT, "kind": _TEXT, "title": _TEXT,
    "completeness": _enum("descriptor", "preview"), "trust_class": {"const": "semantic_owner_descriptor"},
    "preview": {"type": "string"}, "match_provenance": _array(_HISTORICAL_MATCH_PROVENANCE)},
    required=("id", "kind", "title", "completeness", "trust_class"))
_CAPTURE_DESCRIPTOR = _closed({"id": _TEXT, "kind": {"const": "capture"}, "title": _TEXT,
    "capture_kind": _enum("assignment", "output"), "origin_epoch_id": _nullable(_TEXT),
    "origin_epoch_state": _enum("unbound_to_epoch", "epoch_not_terminal", "checkpointed", "failed_before_checkpoint"),
    "late_classification": _enum("unbound_to_epoch", "epoch_not_terminal", "at_or_before_terminal", "after_checkpoint_terminal", "after_failed_terminal"),
    "cut_relation": _enum("no_checkpoint", "at_or_before_latest_checkpoint", "after_latest_checkpoint"),
    "pending_state": _enum("current_pending", "fully_covered"),
    "native_lineage": _nullable(_closed({key: _TEXT for key in ("parent_thread_id", "child_thread_id", "root_thread_id")}, required=())),
    "artifacts": _array(_closed({"handle": _TEXT, "ordinal": {"type": "integer", "minimum": 0}, "role": _TEXT, "logical_name": _TEXT, "pending": {"type": "boolean"}})),
    "failed_interval_or_late_output": {"type": "boolean"}, "trust_class": {"const": "custody_descriptor"}, "completeness": {"const": "descriptor"},
    "match_provenance": _array(_HISTORICAL_MATCH_PROVENANCE)},
    required=("id", "kind", "title", "capture_kind", "origin_epoch_id", "origin_epoch_state", "late_classification", "cut_relation", "pending_state", "native_lineage", "artifacts", "failed_interval_or_late_output", "trust_class", "completeness"))
_ROOT_QUERY_ITEMS = {
    "read": _ROOT_READ_OUTCOME,
    "search": {"oneOf": [_DESCRIPTOR, _CAPTURE_DESCRIPTOR]},
    "inventory": {"oneOf": [_DESCRIPTOR, _CAPTURE_DESCRIPTOR]},
    "checkpoint": {"oneOf": [
        _closed({**_CHECKPOINT_SUMMARY["properties"], "counts": _closed({key: {"type": "integer", "minimum": 0} for key in ROOT_CHECKPOINT_SECTIONS if key != "summary"})}),
        _closed({"id": _TEXT, "revision": {"type": "integer", "minimum": 1}, "handle": _TEXT}),
        _closed({"owner_handle": _TEXT, "json_pointer": _TEXT}),
        _closed({"capture_handle": _TEXT, "artifact_handle": _TEXT, "role": _TEXT, "logical_name": _TEXT}),
    ]},
    "changes_since_checkpoint": _closed({"kind": _enum(*ROOT_RETRIEVAL_KINDS), "id": _TEXT, "before_handle": _nullable(_TEXT), "current_handle": _nullable(_TEXT), "changes": _array(_enum("new", "advanced", "retired"), nonempty=True)}),
    "hooks": _closed({"hook_class": _enum(*ROOT_HOOK_CLASSES), "owner_handle": _TEXT, "semantic_field": _TEXT, "json_pointer": _TEXT, "value": {}, "strategy_connections": _array(_STRATEGY_CONNECTION)}),
    "captures": _CAPTURE_DESCRIPTOR,
    "proof_attention": _OPEN_A1_ATTENTION,
}
_ROOT_RETRIEVAL_RESULT = {"oneOf": [
    _closed({"mode": {"const": mode}, "purpose": _TEXT,
        "scope": _closed({"basis": _enum("exact_selection", "immutable_checkpoint", "current_at_first_page", "live_proof_attention"),
                          "checkpoint_id": _nullable(_TEXT), "selection": _ROOT_RETRIEVE_INPUT_SCHEMA}),
        "completeness": _enum("page", "exhausted"), "state": _enum("no_checkpoint", "none", "present"),
        "items": _array(_ROOT_QUERY_ITEMS[mode]), "next_cursor": _nullable(_TEXT),
        **({"admitted_result": _nullable(_ADMITTED_RESULT)} if mode == "proof_attention" else {})})
    for mode in ROOT_RETRIEVE_MODES if mode != "publication_result"
]}
_HOST_OWNER_REFERENCE = _closed({
    "kind": _enum("mission", "strategy", "branch", "candidate", "context", "evidence", "session", "capture-annotation"),
    "identity": _TEXT,
    "revision": {"type": "integer", "minimum": 1},
    "payload_sha256": _LOWER_SHA256,
})
_HOST_OPEN_A1 = _closed({
    "candidate_ref": _HOST_OWNER_REFERENCE,
    "retrieval_handle": _TEXT,
    "classification": {"const": "purported_complete_rh_proof_or_disproof"},
    "disposition": _enum("proof", "disproof", "legacy_unspecified"),
    "hold_lifecycle": {"const": "open"},
    "canonical_effect": {"const": "none"},
    "mathematical_effect": {"const": "none"},
    "triage": {},
    "admission_rejection": {},
}, required=("candidate_ref", "retrieval_handle", "classification", "disposition",
             "hold_lifecycle", "canonical_effect", "mathematical_effect"))
_HOST_CURRENT_STATE = _closed({
    "schema_version": {"const": "mathematical_research.mission_host_current_state.v2"},
    "project_id": _TEXT,
    "mission_id": _TEXT,
    "observed_project_commit": {"type": "integer", "minimum": 0},
    "mission": _closed({
        "reference": _HOST_OWNER_REFERENCE,
        "summary": _closed({
            "lifecycle": _TEXT, "effective": {"type": "boolean"},
            "autonomous": {"type": "boolean"}, "fence_reason": {},
            "fenced_at": {}, "strategy_ids": _array(_TEXT),
            "purpose": {"type": "object"}, "execution_policy": {"type": "object"},
            "scientific_context_id": _nullable(_TEXT),
        }),
    }),
    "strategy": _closed({
        "reference": _HOST_OWNER_REFERENCE,
        "summary": _closed({
            "mission_continuation": _enum("continue", "pause", "closeout"),
            "integrated_comparison": _TEXT,
            "formal_requests": _array(_FORMAL_REQUEST),
        }),
    }),
    "checkpoint": _nullable(_closed({
        "reference": _closed({"checkpoint_id": _TEXT, "payload_sha256": _LOWER_SHA256}),
        "project_commit": {"type": "integer", "minimum": 0},
        "authoring_epoch_id": _TEXT,
    })),
    "latest_executive_epoch": _nullable(_closed({
        "executive_epoch_id": _TEXT,
        "state": _enum("authorized", "bound", "checkpointed", "failed_before_checkpoint"),
        "goal_thread_id": _nullable(_TEXT),
        "last_event_project_commit": {"type": "integer", "minimum": 0},
        "checkpoint_ref": {},
        "reconciliation": {},
    })),
    "open_candidate_a1": _array(_HOST_OPEN_A1),
    "admitted_result": _nullable(_ADMITTED_RESULT),
    "canonical_authority": _closed({
        "source_commit": {"type": "string", "pattern": r"^[0-9a-f]{40}(?![\s\S])"},
        "canonical_state_sha256": _LOWER_SHA256,
        "canonical_authority_digest": _LOWER_SHA256,
    }),
})
_HOST_AUTHORIZATION_CUT = _closed({
    "project_commit": {"type": "integer", "minimum": 0},
    "current_root_digest": _LOWER_SHA256,
    "transition_head_digest": _nullable(_LOWER_SHA256),
    "canonical_authority_digest": _LOWER_SHA256,
})


def executive_orientation_schema() -> Mapping[str, Any]:
    return deep_freeze(deep_thaw(_EXECUTIVE_ORIENTATION))


def mission_host_snapshot_schema() -> Mapping[str, Any]:
    return deep_freeze(_closed({"schema_version": {"const": "mathematical_research.mission_host_snapshot.v4"},
        "authorization_cut": _HOST_AUTHORIZATION_CUT,
        "current_state": _HOST_CURRENT_STATE, "executive_orientation": _EXECUTIVE_ORIENTATION}))


def validate_mission_host_authorization_cut(
    value: Mapping[str, Any],
) -> Mapping[str, Any]:
    """Validate the private Host cut shape without asserting current freshness."""

    schema = deep_thaw(_HOST_AUTHORIZATION_CUT)
    violations = _schema_violations(value, schema, schema, "$")
    if violations:
        location, message = violations[0]
        raise MissionOperationContractError(
            "mission_host_authorization_cut_invalid",
            location,
            message,
        )
    return deep_freeze(deep_thaw(value))


def validate_executive_orientation(value: Mapping[str, Any]) -> Mapping[str, Any]:
    schema = deep_thaw(executive_orientation_schema())
    violations = _schema_violations(value, schema, schema, "$")
    if violations:
        location, message = violations[0]
        raise MissionOperationContractError("executive_orientation_invalid", location, message)
    try:
        _validate_scientific_orientation_relationships(value)
    except (KeyError, IndexError, TypeError, ValueError) as exc:
        raise MissionOperationContractError(
            "executive_orientation_invalid", "$.scientific_context", str(exc),
        ) from exc
    return deep_freeze(deep_thaw(value))


def _validate_scientific_orientation_relationships(value: Mapping[str, Any]) -> None:
    """Validate closed projection relationships, without judging the science.

    Treatment bodies are inline. Qualification reuse checks a direct same-response
    location, the full exact current source reference and its authored selection.
    The producer additionally requires identical complete qualification content.
    It never resolves a reference using a second owner read or follows chains.
    """
    science = value["scientific_context"]
    binding, root = science["binding"], science["root_reference"]
    if (None if binding is None else binding["context_id"]) != value["mission"]["scientific_context_id"]:
        raise ValueError("scientific Context binding differs from the Mission")
    if root is not None and (binding is None or root["identity"] != binding["context_id"]):
        raise ValueError("scientific Context root differs from the binding")
    if science["state"] == "unbound":
        if any(science[field] is not None for field in ("binding", "root_reference", "purpose", "question", "independence_treatment")) or any(science[field] for field in ("treatments", "source_changes", "unavailable", "known_omissions", "restricted_uses", "restrictions")):
            raise ValueError("unbound scientific Context contains selected content")
        return
    if binding is None:
        raise ValueError("selected scientific Context has no binding")
    if science["state"] == "unavailable":
        if science["purpose"] is not None or science["question"] is not None or science["independence_treatment"] is not None or any(science[field] for field in ("treatments", "source_changes", "known_omissions", "restricted_uses", "restrictions")) or not science["unavailable"]:
            raise ValueError("unavailable scientific root has inconsistent content")
    elif root is None or science["purpose"] is None or science["question"] is None or science["independence_treatment"] is None or ((science["state"] == "available") != (not science["unavailable"])):
        raise ValueError("scientific Context availability differs from its content")

    def pointer(reference: Mapping[str, Any]) -> list[str]:
        text = reference["orientation_path"]
        if not text.startswith("/"):
            raise ValueError("same-response reference must be a JSON pointer")
        import re
        if re.search(r"~(?![01])", text):
            raise ValueError("same-response reference has invalid escaping")
        return [part.replace("~1", "/").replace("~0", "~") for part in text[1:].split("/")]

    def index(part: str) -> int:
        if not part.isascii() or not part.isdigit() or str(int(part)) != part:
            raise ValueError("same-response reference requires an exact array index")
        return int(part)

    treatments: dict[tuple[Any, ...], Mapping[str, Any]] = {}
    for entry in science["treatments"]:
        is_deeper = context_reference_key(entry["context_reference"]) != context_reference_key(root)
        if ("context_qualifications" in entry) != is_deeper:
            raise ValueError("only deeper exposed treatments require enclosing Context qualifications")
        key = (*context_reference_key(entry["context_reference"]), entry["treatment_id"])
        if key in treatments:
            raise ValueError("scientific exposure repeats an exact treatment")
        content = entry["content"]
        _validate_scientific_treatment(content, "scientific treatment")
        treatments[key] = content
    seen: set[tuple[Any, ...]] = set()
    for change in science["source_changes"]:
        cited, current, selection = change["cited_reference"], change["current_reference"], change["selection"]
        validate_context_source_selection(cited, selection)
        selection_key = None if selection is None else (selection["mode"], tuple(selection.get("treatment_ids", ())))
        key = (*context_reference_key(cited), selection_key)
        if key in seen:
            raise ValueError("source change repeats an exact source selection")
        seen.add(key)
        if current is not None and (current["kind"], current["identity"]) != (cited["kind"], cited["identity"]):
            raise ValueError("source change substitutes another source identity")
        for affected in change["affected"]:
            treatment_key = (*context_reference_key(affected["context_reference"]), affected["treatment_id"])
            treatment = treatments.get(treatment_key)
            if treatment is None or not {"recognition", "reliance"}.intersection(affected["roles"]):
                raise ValueError("source change names an unexposed or history-only use")
            if not any(source["reference"] == cited and source["selection"] == selection and set(affected["roles"]) == set(source["roles"]) for source in treatment["sources"]):
                raise ValueError("source change differs from the treatment's exact authored use")
        if change["state"] != "advanced_same_identity":
            if (change["state"] == "no_current_head" and current is not None) or change["qualification"] is not None or change["qualification_ref"] is not None:
                raise ValueError("unavailable source has inconsistent qualification")
            continue
        if current is None or current["revision"] == cited["revision"] or ((change["qualification"] is None) == (change["qualification_ref"] is None)):
            raise ValueError("advanced source requires exactly one current qualification")
        qualification = change["qualification"]
        if change["qualification_ref"] is not None:
            path = pointer(change["qualification_ref"])
            if len(path) == 4 and path[:2] == ["scientific_context", "source_changes"] and path[3] == "qualification":
                other = science["source_changes"][index(path[2])]
                if other["qualification"] is None or other["qualification_ref"] is not None or other["current_reference"] != current or other["selection"] != selection:
                    raise ValueError("qualification reference is chained or changes source selection")
                qualification = other["qualification"]
            else:
                raise ValueError("qualification reference must name one direct exact source projection")
        if current["kind"] == "context":
            if not isinstance(qualification, Mapping) or qualification.get("selection") != selection:
                raise ValueError("Context qualification changed selected use")
            if selection["mode"] == "treatments":
                selected = qualification.get("treatments")
                if not isinstance(selected, Mapping) or not selected or set(selected) - set(selection["treatment_ids"]):
                    raise ValueError("Context qualification escaped or omitted selected treatments")
                for treatment in selected.values():
                    _validate_scientific_treatment(treatment, "source qualification treatment")
                for missing in set(selection["treatment_ids"]) - set(selected):
                    if not any(item["kind"] == "context" and item["context_id"] == current["identity"] and item["treatment_id"] == missing for item in science["unavailable"]):
                        raise ValueError("missing selected qualification lacks exact unavailability")
            elif "context" not in qualification:
                raise ValueError("whole-Context qualification lacks its declared body")
        else:
            selected_schema = deep_thaw(
                _SOURCE_EVIDENCE_QUALIFICATION
                if current["kind"] == "evidence"
                else _SOURCE_OWNER_QUALIFICATION_SCHEMAS[current["kind"]]
            )
            if not isinstance(qualification, Mapping) or not qualification or _schema_violations(
                qualification, selected_schema, selected_schema, "$.qualification",
            ):
                raise ValueError("source qualification differs from its declared owner projection")


def validate_mission_host_snapshot(value: Mapping[str, Any]) -> Mapping[str, Any]:
    schema = deep_thaw(mission_host_snapshot_schema())
    violations = _schema_violations(value, schema, schema, "$")
    if violations:
        location, message = violations[0]
        raise MissionOperationContractError("mission_host_snapshot_invalid", location, message)
    frozen = deep_freeze(deep_thaw(value))
    validate_executive_orientation(value["executive_orientation"])
    cut = frozen["authorization_cut"]
    current_state = frozen["current_state"]
    if cut["project_commit"] != current_state["observed_project_commit"]:
        raise MissionOperationContractError(
            "mission_host_snapshot_invalid",
            "$.authorization_cut.project_commit",
            "must equal $.current_state.observed_project_commit",
        )
    if (
        cut["canonical_authority_digest"]
        != current_state["canonical_authority"]["canonical_authority_digest"]
    ):
        raise MissionOperationContractError(
            "mission_host_snapshot_invalid",
            "$.authorization_cut.canonical_authority_digest",
            "must equal the current canonical authority digest",
        )
    return frozen


_SCIENTIFIC_PUBLICATION_RESULT = _closed({
    "schema_version": {"const": "mathematical_research.scientific_publication_result.v1"},
    "command_id": _TEXT, "project_commit": {"type": "integer", "minimum": 0},
    "replayed": {"type": "boolean"}, "semantic_noop": {"type": "boolean"},
    "evidence_reference": _nullable(_closed({
        **_EXACT_OWNER_REFERENCE["properties"], "kind": {"const": "evidence"},
    })),
    "context_references": _array(_EXACT_CONTEXT_REFERENCE),
    "changed_treatments": _array(_closed({
        "context_reference": _EXACT_CONTEXT_REFERENCE, "treatment_id": _TEXT,
        "content": _nullable(_SCIENTIFIC_TREATMENT),
    })),
    "exposure_required": _array(_SCIENTIFIC_EXPOSURE),
})
_READ_RESULTS: Mapping[str, Mapping[str, Any]] = {
    ORIENT: {
        "oneOf": [
            _closed({"orientation": _EXECUTIVE_ORIENTATION}),
            _HISTORICAL_READ_RESULT,
        ]
    },
    RETRIEVE: {
        "oneOf": [
            _ROOT_RETRIEVAL_RESULT,
            _HISTORICAL_READ_RESULT,
            _SCIENTIFIC_PUBLICATION_RESULT,
        ]
    },
}
_WRITE_RESULT = _closed(
    {
        "record_id": _TEXT,
        "revision": {"type": "integer", "minimum": 1},
        "semantic_summary": _TEXT,
    },
)
_PUBLICATION_WRITE_RESULT = _closed({
    **_WRITE_RESULT["properties"], "publication": _SCIENTIFIC_PUBLICATION_RESULT,
}, required=("record_id", "revision", "semantic_summary"))
_FORMAL_REQUEST_RESULT = _closed(
    {
        "selected_bet_sha256": _LOWER_SHA256,
        "bet": _TEXT,
        "discriminator": _TEXT,
        "purpose": _enum("targeted_falsification", "targeted_verification"),
        "context_retrieval_handle": _TEXT,
    }
)
_STRATEGY_WRITE_RESULT = _closed(
    {
        "record_id": _TEXT,
        "revision": {"type": "integer", "minimum": 1},
        "semantic_summary": _TEXT,
        "formal_requests": _array(_FORMAL_REQUEST_RESULT),
    }
)
_CANDIDATE_A1_RESULT = _closed(
    {
        "candidate_ref": _closed(
            {
                "kind": {"const": "candidate"},
                "identity": _TEXT,
                "revision": {"type": "integer", "minimum": 1},
                "payload_sha256": _LOWER_SHA256,
            }
        ),
        "retrieval_handle": _TEXT,
        "classification": _enum("purported_complete_rh_proof_or_disproof"),
        "disposition": _enum("proof", "disproof", "legacy_unspecified"),
        "hold_lifecycle": _enum("open"),
    }
)
_CANDIDATE_WRITE_RESULT = _closed(
    {
        "record_id": _TEXT,
        "revision": {"type": "integer", "minimum": 1},
        "semantic_summary": _TEXT,
        "open_candidate_a1": _nullable(_CANDIDATE_A1_RESULT),
    }
)
_INTERPRET_RESULT = _closed(
    {
        "records": _array(_PUBLICATION_WRITE_RESULT, nonempty=True),
        "semantic_summary": _TEXT,
        "publication": _SCIENTIFIC_PUBLICATION_RESULT,
    }, required=("records", "semantic_summary"),
)
_SYNTHESIS_REJECTION = _closed(
    {
        "consequence_index": {"type": "integer", "minimum": 0},
        "message": _TEXT,
    }
)
_SYNTHESIS_RESULT = _closed(
    {
        "records": _array(_PUBLICATION_WRITE_RESULT),
        "rejections": _array(_SYNTHESIS_REJECTION),
        "semantic_summary": _TEXT,
    }
)
_CHECKPOINT_RESULT = _closed(
    {
        "checkpoint_id": _TEXT,
        "executive_epoch_id": _TEXT,
        "state": _enum("checkpointed"),
    }
)
_RESULT_SCHEMAS: Mapping[str, Mapping[str, Any]] = {
    **_READ_RESULTS,
    RECORD_CONTEXT: {"oneOf": [_WRITE_RESULT, _SCIENTIFIC_PUBLICATION_RESULT]},
    RECORD_CANDIDATE: _CANDIDATE_WRITE_RESULT,
    RECORD_STRATEGY: _STRATEGY_WRITE_RESULT,
    INTERPRET_MATERIAL: _INTERPRET_RESULT,
    SYNTHESIZE: _SYNTHESIS_RESULT,
    CHECKPOINT: _CHECKPOINT_RESULT,
}

_COMMON_ERRORS = (
    {
        "code": "mission_operation_request_invalid",
        "property": "request_shape",
        "failure_scope": "call",
        "correction": "revise_request",
    },
    {
        "code": "mission_operation_owner_fact_forbidden",
        "property": "authority_separation",
        "failure_scope": "call",
        "correction": "remove_owner_fact",
    },
    {
        "code": "mission_operation_not_allowed",
        "property": "operation_authorization",
        "failure_scope": "call",
        "correction": "choose_authorized_operation",
    },
    {
        "code": "mission_operation_state_conflict",
        "property": "current_owner_state",
        "failure_scope": "call",
        "correction": "refresh_then_rejudge_if_semantics_changed",
    },
    {
        "code": "mission_operation_unavailable",
        "property": "interface_availability",
        "failure_scope": "operation",
        "correction": "repair_owner_interface",
    },
    {
        "code": "executive_epoch_unavailable",
        "property": "executive_authority",
        "failure_scope": "goal",
        "correction": "authorize_fresh_goal",
    },
    {
        "code": "mission_authorization_expired",
        "property": "mission_authority",
        "failure_scope": "operation",
        "correction": "renew_mission_authorization",
    },
    {
        "code": "mission_fenced",
        "property": "shared_operational_safety",
        "failure_scope": "mission",
        "correction": "owner_reconciliation_required",
    },
)

_FACTS = {
    "read": (
        "mission_epoch_and_authority_bindings",
        "current_owner_state",
        "resolved_identity_version_and_access",
        "integrity_and_provenance",
    ),
    "write": (
        "command_and_idempotency_identity",
        "mission_epoch_goal_thread_and_actor_bindings",
        "current_owner_state_and_revisions",
        "resolved_reference_and_custody_facts",
        "host_time",
        "writer_lease",
    ),
}


def _operation(
    operation: str,
    verb: str,
    purpose: str,
    effect_class: str,
    read_only: bool,
    preconditions: Sequence[str],
    extra_errors: Sequence[Mapping[str, str]],
) -> dict[str, Any]:
    return {
        "operation": operation,
        "cli_verb": verb,
        "purpose": purpose,
        "effect": {
            "class": effect_class,
            "read_only": read_only,
            "canonical": "none",
            "public": "none",
            "provider": "none",
            "credential": "none",
            "destructive": "none",
        },
        "preconditions": list(preconditions),
        "semantic_input_schema": deep_thaw(_INPUT_SCHEMAS[operation]),
        "owner_fact_injection": list(_FACTS["read" if read_only else "write"]),
        "result_content_schema": deep_thaw(
            _RESULT_SCHEMAS.get(operation, _WRITE_RESULT)
        ),
        "errors": [deep_thaw(item) for item in (*_COMMON_ERRORS, *extra_errors)],
        "interface_dispatch": {
            "execute": MISSION_INTERFACE_EXECUTE_METHOD,
            "preview": None if read_only else MISSION_INTERFACE_PREVIEW_METHOD,
        },
    }


_OPERATIONS = (
    _operation(
        ORIENT,
        "orient",
        "Receive or refresh the current decision-relevant Executive orientation. "
        "Root orient supplies the Mission, independent Mission-wide scientific Context, "
        "current Strategy, direct Strategy ground, "
        "proof attention and formal attention, not a capture or all-owner inventory.",
        "read_only",
        True,
        ("bound_mission", "readable_mission_state"),
        (),
    ),
    _operation(
        RETRIEVE,
        "retrieve",
        (
            "Search or read authorized exact research material losslessly. Search by "
            "an exact returned child-thread id to resolve its automatically captured "
            "native result to capture:<id> and its artifact handle. For the root console, "
            "bounded inventory, checkpoint, changes_since_checkpoint, hooks and captures "
            "provide additional handles and descriptors; proof_attention selects proof attention. "
            "Search defaults preserve all declared kinds and fields; contribution-centered "
            "mathematical inquiries explicitly select applicable mathematical kinds. "
            "Capture search is metadata-only. Exact publication_result lookup authenticates "
            "the original committed group, not the latest heads. "
            "Use captures with exact thread_ids filters and page_size/cursor paging when "
            "selecting several capture descriptors. Read consumes only exact selected handles."
        ),
        "read_only",
        True,
        ("bound_mission", "authorized_material_scope"),
        (
            {
                "code": "mission_material_not_found",
                "property": "selected_material",
                "failure_scope": "call",
                "correction": "revise_selector_or_search",
            },
            {
                "code": "mission_material_unavailable",
                "property": "selected_material_availability",
                "failure_scope": "call",
                "correction": "continue_other_material_and_report_exact_artifact",
            },
            *({"code": code, "property": "root_retrieval_selection", "failure_scope": "call", "correction": "refresh_exact_query_without_changing_research_state"} for code in (
                "mission_retrieval_cursor_invalid", "mission_retrieval_context_invalid",
                "mission_retrieval_context_required", "mission_retrieval_wrong_mission",
                "mission_retrieval_not_found",
            )),
        ),
    ),
    _operation(
        RECORD_CONTEXT,
        "record-context",
        (
            "Record consequential Context relevance, limits, and dependencies. One "
            "question-shaped Context may ground several workers; exploratory work "
            "needs Context only if it becomes relied on. Scientific mode applies named "
            "create/patch updates against exact heads; unmentioned treatments survive. "
            "Explicit exposure_required guarantees only those post-publication root/deeper "
            "treatments. Exact source roles, selected Context treatments and qualifications "
            "remain distinct from Strategy selection and mathematical proof."
        ),
        "noncanonical_context_state",
        False,
        ("live_mission_authority", "open_executive_epoch", "root_semantic_actor"),
        (
            {
                "code": "mission_context_rejected",
                "property": "context_domain_invariants",
                "failure_scope": "call",
                "correction": "revise_context_judgment",
            },
        ),
    ),
    _operation(
        INTERPRET_MATERIAL,
        "interpret-material",
        (
            "Attach scoped executive meaning to already preserved material. One "
            "Evidence meaning may cite several capture scopes; unrelied captures may "
            "remain pending without an annotation. Optional scientific_continuation with "
            "an exact expected Evidence head pairs only this Evidence, its authored "
            "unfinished question and necessary automatic exposure; ordinary Evidence "
            "does not require Context changes."
        ),
        "noncanonical_evidence_interpretation",
        False,
        (
            "live_mission_authority",
            "open_executive_epoch",
            "root_semantic_actor",
            "material_already_preserved",
        ),
        (
            {
                "code": "mission_material_not_preserved",
                "property": "material_custody",
                "failure_scope": "call",
                "correction": "wait_for_or_repair_capture_owner",
            },
        ),
    ),
    _operation(
        RECORD_CANDIDATE,
        "record-candidate",
        "Record one definite executive-authored mathematical proposal.",
        "noncanonical_candidate_state",
        False,
        ("live_mission_authority", "open_executive_epoch", "root_semantic_actor"),
        (
            {
                "code": "mission_candidate_rejected",
                "property": "candidate_domain_invariants",
                "failure_scope": "call",
                "correction": "revise_candidate_judgment",
            },
        ),
    ),
    _operation(
        RECORD_BRANCH,
        "record-branch",
        "Form or revise one independent mathematical Discovery Branch.",
        "noncanonical_branch_state",
        False,
        ("live_mission_authority", "open_executive_epoch", "root_semantic_actor"),
        (
            {
                "code": "mission_branch_rejected",
                "property": "branch_domain_invariants",
                "failure_scope": "call",
                "correction": "revise_branch_judgment",
            },
        ),
    ),
    _operation(
        SYNTHESIZE,
        "synthesize",
        (
            "Relate exact interpreted meanings and write only explicit ordinary "
            "consequences. Consequences are independent: inspect both records and "
            "indexed rejections, keep accepted records, and never retry the whole "
            "call to correct one rejected consequence. One Evidence consequence may "
            "explicitly pair scientific_continuation with exact expected heads; a failure "
            "is local to that group and never delays complete-target Candidate/A1 preservation."
        ),
        "noncanonical_research_state",
        False,
        ("live_mission_authority", "open_executive_epoch", "root_semantic_actor"),
        (
            {
                "code": "mission_synthesis_rejected",
                "property": "synthesis_domain_invariants",
                "failure_scope": "call",
                "correction": "revise_synthesis_judgment",
            },
        ),
    ),
    _operation(
        RECORD_STRATEGY,
        "record-strategy",
        (
            "Revise the single Mission-wide causal research Strategy. When owner "
            "material changes the decision, cite the exact relied-on owners through "
            "causal_inputs, context_treatment, or owner_refs; empty collections remain "
            "valid when nothing was relied on. Ordinary Executive authoring records "
            "continue; only the exact admitted-result projection records semantic "
            "closeout. Operational stop and suspension do not rewrite Strategy."
        ),
        "noncanonical_mission_state",
        False,
        ("live_mission_authority", "open_executive_epoch", "root_semantic_actor"),
        (
            {
                "code": "mission_strategy_rejected",
                "property": "strategy_domain_invariants",
                "failure_scope": "call",
                "correction": "revise_strategy_judgment",
            },
        ),
    ),
    _operation(
        CHECKPOINT,
        "checkpoint",
        (
            "End the bound Executive Epoch with one proof-neutral owner-resolved "
            "handoff, only after durable work or a truthful Strategy decision."
        ),
        "noncanonical_mission_state",
        False,
        ("live_mission_authority", "open_executive_epoch", "root_semantic_actor"),
        (
            {
                "code": "mission_checkpoint_rejected",
                "property": "checkpoint_domain_invariants",
                "failure_scope": "call",
                "correction": "continue_epoch_before_checkpoint",
            },
        ),
    ),
)
_BY_ID = {item["operation"]: item for item in _OPERATIONS}
_BY_VERB = {item["cli_verb"]: item for item in _OPERATIONS}
if tuple(_BY_ID) != MISSION_OPERATION_ORDER or len(_BY_VERB) != len(_OPERATIONS):
    raise RuntimeError("Mission operation identities are not exact and unique")

_CONTRACT = {
    "schema_version": MISSION_OPERATION_CONTRACT_SCHEMA_VERSION,
    "stability": "noncanonical_runtime",
    "activation_state": "active_source_contract",
    "authority_owner": "mathematical_research.research_core",
    "request_schema_version": MISSION_SEMANTIC_REQUEST_SCHEMA_VERSION,
    "result_schema_version": MISSION_SEMANTIC_RESULT_SCHEMA_VERSION,
    "operations": list(_OPERATIONS),
}
_FROZEN_CONTRACT = deep_freeze(_CONTRACT)


def mission_operation_contract() -> Mapping[str, Any]:
    return _FROZEN_CONTRACT


def mission_operation_ids() -> tuple[str, ...]:
    return MISSION_OPERATION_ORDER


def mission_operation(operation: str) -> Mapping[str, Any]:
    try:
        return deep_freeze(_BY_ID[operation])
    except KeyError:
        raise MissionOperationContractError(
            "mission_operation_not_allowed",
            "operation",
            f"Unsupported Mission semantic operation: {operation!r}",
        ) from None


def operation_from_cli_verb(verb: str) -> str:
    try:
        return str(_BY_VERB[verb]["operation"])
    except KeyError:
        raise MissionOperationContractError(
            "mission_operation_not_allowed",
            "verb",
            f"Unsupported Mission semantic CLI verb: {verb!r}",
        ) from None


def _subset(allowed_operations: Iterable[str] | None) -> tuple[str, ...]:
    if allowed_operations is None:
        return MISSION_OPERATION_ORDER
    supplied = tuple(allowed_operations)
    if not supplied or len(set(supplied)) != len(supplied):
        raise MissionOperationContractError(
            "mission_operation_request_invalid",
            "allowed_operations",
            "Operation subset must be nonempty and duplicate-free.",
        )
    unknown = [item for item in supplied if item not in _BY_ID]
    if unknown:
        raise MissionOperationContractError(
            "mission_operation_not_allowed",
            "allowed_operations",
            "Unsupported operation subset member: " + ", ".join(map(repr, unknown)),
        )
    selected = set(supplied)
    return tuple(item for item in MISSION_OPERATION_ORDER if item in selected)


def historical_read_grant_input_schema() -> Mapping[str, Any]:
    """Return the closed root-only input used to request one delegated grant."""

    return deep_freeze(
        _closed(
            {
                "child_thread_id": _TEXT,
                "assignment_mode": _enum(*HISTORICAL_ASSIGNMENT_MODES),
                "assignment": _TEXT,
                "context": _closed(
                    {
                        "id": _EXISTING_CONTEXT_RECORD_ID,
                        "revision": {"type": "integer", "minimum": 1},
                    }
                ),
                "source_families": _HISTORICAL_SOURCE_FAMILIES,
                "raw_body_policy": _enum(*HISTORICAL_RAW_BODY_POLICIES),
            }
        )
    )


def research_read_grant_input_schema() -> Mapping[str, Any]:
    """Bind an ordinary scientific assignment, separately from frozen advice/review."""
    return deep_freeze(_closed({
        "child_thread_id": _TEXT,
        "assignment": _TEXT,
        "source_families": _array(_enum(*RESEARCH_SOURCE_FAMILIES), nonempty=True),
        "raw_body_policy": _enum(*HISTORICAL_RAW_BODY_POLICIES),
    }))


def research_read_selection_schema() -> Mapping[str, Any]:
    return deep_freeze({"oneOf": [
        *[deep_thaw(item) for item in _ROOT_RETRIEVE_INPUT_SCHEMA["oneOf"]
          if item["properties"]["mode"]["const"] in {"read", "search"}],
        *[deep_thaw(item) for item in _HISTORICAL_RETRIEVE_INPUT_SCHEMA["oneOf"]
          if item["properties"]["mode"]["const"] in HISTORICAL_RETRIEVE_MODES],
        _closed({"mode": {"const": "selected_context"}, "purpose": _TEXT,
                 "id": {"type": "string", "pattern": r"^context:[A-Za-z0-9][A-Za-z0-9._:-]{0,191}@[1-9][0-9]*(?![\s\S])"},
                 "selection": _CONTEXT_SOURCE_SELECTION}),
    ]})


def research_read_request_schema() -> Mapping[str, Any]:
    return deep_freeze({"oneOf": [
        _closed({"mode": {"const": "usage"},
                 "topics": _array(_enum(*RESEARCH_READ_USAGE_TOPICS), nonempty=True)},
                required=("mode",)),
        _closed({"mode": {"const": "retrieve"},
                 "selection": research_read_selection_schema()}),
    ]})


def _validate_research_read_value(
    value: Mapping[str, Any], schema: Mapping[str, Any], *, code: str,
) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise MissionOperationContractError(code, "$", "Research read input must be one object.")
    document = deep_thaw(schema)
    violations = _schema_violations(value, document, document, "$")
    if violations:
        location, message = violations[0]
        raise MissionOperationContractError(code, location, message)
    return deep_freeze(deep_thaw(value))


def validate_research_read_grant_request(value: Mapping[str, Any]) -> Mapping[str, Any]:
    return _validate_research_read_value(value, research_read_grant_input_schema(),
                                         code="research_read_grant_request_invalid")


def validate_research_read_request(value: Mapping[str, Any]) -> Mapping[str, Any]:
    return _validate_research_read_value(value, research_read_request_schema(),
                                         code="research_read_request_invalid")


def research_read_result_schema() -> Mapping[str, Any]:
    envelope = {"schema_version": {"const": "mathematical_research.research_read_result.v1"},
                "grant_id": _LOWER_SHA256, "assignment_id": _TEXT,
                "project_commit_cut": {"type": "integer", "minimum": 0}}
    return deep_freeze({"oneOf": [
        _closed({**envelope, "mode": {"const": "usage"}, "result": _closed({
            "topics": _closed({topic: _TEXT for topic in RESEARCH_READ_USAGE_TOPICS}, required=()),
            "selection_schema": {"type": "object"},
            "source_families": _array(_enum(*RESEARCH_SOURCE_FAMILIES), nonempty=True),
            "raw_body_policy": _enum(*HISTORICAL_RAW_BODY_POLICIES),
        })}),
        _closed({**envelope, "mode": {"const": "retrieve"}, "result": {"oneOf": [
            *[deep_thaw(item) for item in _ROOT_RETRIEVAL_RESULT["oneOf"]
              if item["properties"]["mode"]["const"] in {"read", "search"}],
            _HISTORICAL_READ_RESULT,
            _closed({"mode": {"const": "selected_context"}, "purpose": _TEXT,
                     "items": _array(_ROOT_READ_OUTCOME), "next_cursor": {"type": "null"}}),
        ]}}),
    ]})


def validate_research_read_result(value: Mapping[str, Any]) -> Mapping[str, Any]:
    return _validate_research_read_value(value, research_read_result_schema(),
                                         code="research_read_result_invalid")


def validate_historical_read_grant_request(
    value: Mapping[str, Any],
) -> Mapping[str, Any]:
    """Validate one model-originating root grant request without owner facts."""

    if not isinstance(value, Mapping):
        raise MissionOperationContractError(
            "historical_read_grant_request_invalid",
            "$",
            "Historical read grant request must be one object.",
        )
    schema = deep_thaw(historical_read_grant_input_schema())
    violations = _schema_violations(value, schema, schema, "$")
    if violations:
        location, message = violations[0]
        raise MissionOperationContractError(
            "historical_read_grant_request_invalid",
            location,
            message,
        )
    return deep_freeze(deep_thaw(value))


def candidate_a1_review_grant_input_schema() -> Mapping[str, Any]:
    """Return the closed root input for one exact direct-child A1 review."""

    return deep_freeze(
        _closed(
            {
                "child_thread_id": _TEXT,
                "assignment": _TEXT,
                "context": _closed(
                    {
                        "id": _EXISTING_CONTEXT_RECORD_ID,
                        "revision": {"type": "integer", "minimum": 1},
                    }
                ),
                "candidate_ref": _closed(
                    {
                        "id": _EXISTING_CANDIDATE_RECORD_ID,
                        "revision": {"type": "integer", "minimum": 1},
                        "payload_sha256": _LOWER_SHA256,
                    }
                ),
            }
        )
    )


def validate_candidate_a1_review_grant_request(
    value: Mapping[str, Any],
) -> Mapping[str, Any]:
    """Validate one root-originating A1 review grant request without owner facts."""

    if not isinstance(value, Mapping):
        raise MissionOperationContractError(
            "candidate_a1_review_grant_request_invalid",
            "$",
            "Candidate A1 review grant request must be one object.",
        )
    schema = deep_thaw(candidate_a1_review_grant_input_schema())
    violations = _schema_violations(value, schema, schema, "$")
    if violations:
        location, message = violations[0]
        raise MissionOperationContractError(
            "candidate_a1_review_grant_request_invalid",
            location,
            message,
        )
    return deep_freeze(deep_thaw(value))


def candidate_a1_review_request_schema() -> Mapping[str, Any]:
    """Return the closed child request union for the separate A1 review plane."""

    evidence_ref = _closed(
        {
            "id": _EVIDENCE_RECORD_ID,
            "revision": {"type": "integer", "minimum": 1},
            "payload_sha256": _LOWER_SHA256,
        }
    )
    concrete_defect = _closed(
        {
            "exact_defect": _TEXT,
            "affected_scope": _TEXT,
            "sufficiency_basis": _TEXT,
        }
    )
    return deep_freeze(
        {
            "oneOf": [
                _closed({"mode": {"const": "usage"}}),
                _closed(
                    {
                        "mode": {"const": "retrieve"},
                        "page_size": {
                            "type": "integer",
                            "minimum": 1,
                            "maximum": 50,
                        },
                        "cursor": _TEXT,
                    },
                    required=("mode",),
                ),
                _closed(
                    {
                        "mode": {"const": "submit"},
                        "disposition": _enum(*CANDIDATE_A1_REVIEW_DISPOSITIONS),
                        "review_finding": _TEXT,
                        "cited_basis": _array(evidence_ref),
                        "concrete_defects": _array(concrete_defect),
                        "no_remaining_material_objection": {"type": "boolean"},
                        "limitations": _TEXTS,
                        "non_inferences": _TEXTS,
                    },
                    required=(
                        "mode",
                        "disposition",
                        "review_finding",
                        "no_remaining_material_objection",
                    ),
                ),
            ]
        }
    )


def validate_candidate_a1_review_request(
    value: Mapping[str, Any],
) -> Mapping[str, Any]:
    """Validate one child-originating request without accepting caller facts."""

    if not isinstance(value, Mapping):
        raise MissionOperationContractError(
            "candidate_a1_review_request_invalid",
            "$",
            "Candidate A1 review request must be one object.",
        )
    schema = deep_thaw(candidate_a1_review_request_schema())
    violations = _schema_violations(value, schema, schema, "$")
    if violations:
        location, message = violations[0]
        raise MissionOperationContractError(
            "candidate_a1_review_request_invalid",
            location,
            message,
        )
    return deep_freeze(deep_thaw(value))


def _exact_evidence_ref_schema() -> Mapping[str, Any]:
    return _closed(
        {
            "id": _EVIDENCE_RECORD_ID,
            "revision": {"type": "integer", "minimum": 1},
            "payload_sha256": _LOWER_SHA256,
        }
    )


def _admission_owner_revision_ref_schema() -> Mapping[str, Any]:
    return _closed(
        {
            "kind": _enum("evidence", "context", "branch", "candidate"),
            "identity": _TEXT,
            "revision": {"type": "integer", "minimum": 1},
            "payload_sha256": _LOWER_SHA256,
        }
    )


def admission_case_request_schema() -> Mapping[str, Any]:
    return deep_freeze(
        _closed(
            {
                "candidate_ref": _closed(
                    {
                        "id": _EXISTING_CANDIDATE_RECORD_ID,
                        "revision": {"type": "integer", "minimum": 1},
                        "payload_sha256": _LOWER_SHA256,
                    }
                )
            }
        )
    )


def _validate_admission_contract_request(
    value: Mapping[str, Any],
    *,
    schema: Mapping[str, Any],
    code: str,
    label: str,
) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise MissionOperationContractError(code, "$", f"{label} must be one object.")
    thawed = deep_thaw(schema)
    violations = _schema_violations(value, thawed, thawed, "$")
    if violations:
        location, message = violations[0]
        raise MissionOperationContractError(code, location, message)
    return deep_freeze(deep_thaw(value))


def validate_admission_case_request(value: Mapping[str, Any]) -> Mapping[str, Any]:
    return _validate_admission_contract_request(
        value,
        schema=admission_case_request_schema(),
        code="admission_case_request_invalid",
        label="Admission Case request",
    )


def _admission_grant_input_schema() -> Mapping[str, Any]:
    return _closed(
        {
            "child_thread_id": _TEXT,
            "assignment": _TEXT,
            "context": _closed(
                {
                    "id": _EXISTING_CONTEXT_RECORD_ID,
                    "revision": {"type": "integer", "minimum": 1},
                }
            ),
            "case_ref": _exact_evidence_ref_schema(),
        }
    )


def validate_admission_review_grant_request(
    value: Mapping[str, Any],
) -> Mapping[str, Any]:
    return _validate_admission_contract_request(
        value,
        schema=_admission_grant_input_schema(),
        code="admission_review_grant_request_invalid",
        label="Admission Review grant request",
    )


def validate_admission_decision_grant_request(
    value: Mapping[str, Any],
) -> Mapping[str, Any]:
    return _validate_admission_contract_request(
        value,
        schema=_admission_grant_input_schema(),
        code="admission_decision_grant_request_invalid",
        label="Admission Decision grant request",
    )


def _admission_material_objection_schema() -> Mapping[str, Any]:
    return _closed(
        {
            "exact_objection": _TEXT,
            "affected_scope": _TEXT,
            "materiality_basis": _TEXT,
        }
    )


def admission_review_request_schema() -> Mapping[str, Any]:
    return deep_freeze(
        {
            "oneOf": [
                _closed({"mode": {"const": "usage"}}),
                _closed(
                    {
                        "mode": {"const": "retrieve"},
                        "page_size": {"type": "integer", "minimum": 1, "maximum": 50},
                        "cursor": _TEXT,
                    },
                    required=("mode",),
                ),
                _closed(
                    {
                        "mode": {"const": "submit"},
                        "disposition": _enum(*ADMISSION_REVIEW_DISPOSITIONS),
                        "review_finding": _TEXT,
                        "objections": _array(_admission_material_objection_schema()),
                        "cited_basis": _array(
                            _admission_owner_revision_ref_schema()
                        ),
                        "limitations": _TEXTS,
                        "non_inferences": _TEXTS,
                    },
                    required=("mode", "disposition", "review_finding"),
                ),
            ]
        }
    )


def validate_admission_review_request(
    value: Mapping[str, Any],
) -> Mapping[str, Any]:
    return _validate_admission_contract_request(
        value,
        schema=admission_review_request_schema(),
        code="admission_review_request_invalid",
        label="Admission Review request",
    )


def admission_decision_request_schema() -> Mapping[str, Any]:
    return deep_freeze(
        {
            "oneOf": [
                _closed({"mode": {"const": "usage"}}),
                _closed(
                    {
                        "mode": {"const": "retrieve"},
                        "page_size": {"type": "integer", "minimum": 1, "maximum": 50},
                        "cursor": _TEXT,
                    },
                    required=("mode",),
                ),
                _closed(
                    {
                        "mode": {"const": "submit"},
                        "disposition": {"const": "authorize_exact_delta"},
                        "decision_basis": _TEXT,
                        "limitations": _TEXTS,
                        "non_inferences": _TEXTS,
                    },
                    required=("mode", "disposition", "decision_basis"),
                ),
                _closed(
                    {
                        "mode": {"const": "submit"},
                        "disposition": {"const": "reject"},
                        "decision_basis": _TEXT,
                        "objections": _array(
                            _admission_material_objection_schema(), nonempty=True
                        ),
                        "cited_basis": _array(
                            _admission_owner_revision_ref_schema(), nonempty=True
                        ),
                        "limitations": _TEXTS,
                        "non_inferences": _TEXTS,
                    },
                    required=(
                        "mode",
                        "disposition",
                        "decision_basis",
                        "objections",
                        "cited_basis",
                    ),
                ),
            ]
        }
    )


def validate_admission_decision_request(
    value: Mapping[str, Any],
) -> Mapping[str, Any]:
    return _validate_admission_contract_request(
        value,
        schema=admission_decision_request_schema(),
        code="admission_decision_request_invalid",
        label="Admission Decision request",
    )


def admission_host_grant_input_schema() -> Mapping[str, Any]:
    """Project one root-only selector over the two exact Admission grant owners."""

    grant = deep_thaw(_admission_grant_input_schema())
    return deep_freeze(
        _closed(
            {
                "role": _enum(*ADMISSION_HOST_GRANT_ROLES),
                **grant["properties"],
            }
        )
    )


def admission_child_request_schema() -> Mapping[str, Any]:
    """Project one child alias whose live grant selects reviewer or admitter."""

    review = deep_thaw(admission_review_request_schema())["oneOf"]
    decision = deep_thaw(admission_decision_request_schema())["oneOf"]
    return deep_freeze(
        {
            "oneOf": [
                review[0],
                review[1],
                review[2],
                decision[2],
                decision[3],
            ]
        }
    )


def _semantic_request_schema(
    operation: str,
    *,
    historical_read: bool,
) -> Mapping[str, Any]:
    item = mission_operation(operation)
    input_schema = (
        _HISTORICAL_RETRIEVE_INPUT_SCHEMA
        if historical_read and operation == RETRIEVE
        else item["semantic_input_schema"]
    )
    schema = _closed(
        {
            "schema_version": {"const": MISSION_SEMANTIC_REQUEST_SCHEMA_VERSION},
            "operation": {"const": operation},
            "input": input_schema,
        }
    )
    schema["description"] = str(item["purpose"])
    return deep_freeze(schema)


def semantic_request_schema(operation: str) -> Mapping[str, Any]:
    """Return the root-console request schema with legacy retrieve modes only."""

    return _semantic_request_schema(operation, historical_read=False)


def semantic_result_schema(operation: str) -> Mapping[str, Any]:
    item = mission_operation(operation)
    errors = item["errors"]
    error = _closed(
        {
            "code": _enum(*(entry["code"] for entry in errors)),
            "message": _TEXT,
            "location": _TEXT,
            "property": _enum(*(entry["property"] for entry in errors)),
            "failure_scope": _enum(*(entry["failure_scope"] for entry in errors)),
            "correction": _enum(*(entry["correction"] for entry in errors)),
        },
        required=("code", "message", "property", "failure_scope", "correction"),
    )
    common = {
        "schema_version": {"const": MISSION_SEMANTIC_RESULT_SCHEMA_VERSION},
        "operation": {"const": operation},
    }
    return deep_freeze(
        {
            "oneOf": [
                _closed(
                    {
                        **common,
                        "status": {"const": "completed"},
                        "result": item["result_content_schema"],
                        "error": {"type": "null"},
                    }
                ),
                _closed(
                    {
                        **common,
                        "status": _enum("rejected", "unavailable"),
                        "result": {"type": "null"},
                        "error": error,
                    }
                ),
            ]
        }
    )


def _validated_tool_name(tool_name: str) -> str:
    if not isinstance(tool_name, str) or not tool_name.strip():
        raise MissionOperationContractError(
            "mission_operation_projection_invalid",
            "$.tool_name",
            "tool_name must be a non-empty string.",
        )
    return tool_name


def _projected_semantic_input_schema(
    operation: str,
    *,
    historical_read: bool,
    admitted_result_closeout: bool,
) -> Mapping[str, Any]:
    if historical_read and operation == RETRIEVE:
        return _HISTORICAL_RETRIEVE_INPUT_SCHEMA
    schema = mission_operation(operation)["semantic_input_schema"]
    if operation != RECORD_STRATEGY:
        return schema
    projected = deep_thaw(schema)
    projected["properties"]["mission_continuation"]["enum"] = [
        "closeout" if admitted_result_closeout else "continue"
    ]
    return deep_freeze(projected)


def _model_operation_guide(
    operation: str,
    *,
    tool_name: str,
    historical_read: bool,
    admitted_result_closeout: bool,
) -> dict[str, Any]:
    item = mission_operation(operation)
    examples = []
    for example in _MODEL_INPUT_EXAMPLES[operation]:
        example_input = deep_thaw(example["input"])
        example_label = str(example["label"])
        if operation == RECORD_STRATEGY:
            example_input["mission_continuation"] = (
                "closeout" if admitted_result_closeout else "continue"
            )
            if admitted_result_closeout:
                example_label = "Record the exact admitted-result Mission closeout"
                example_input["integrated_comparison"] = (
                    "The exact current admitted result resolves the Mission target."
                )
                example_input["reconsideration_conditions"] = [
                    {
                        "condition": (
                            "Reconsider only if the exact admitted-result authority "
                            "is no longer current."
                        )
                    }
                ]
        examples.append(
            {
                "label": example_label,
                "call": {
                    "operation": operation,
                    "input": example_input,
                },
            }
        )
    return {
        "schema_version": MISSION_MODEL_USAGE_SCHEMA_VERSION,
        "tool": tool_name,
        "status": "ok",
        "command_grammar": '{"operation":"<usage-or-semantic-operation>","input":{...}}',
        "operation": operation,
        "purpose": str(item["purpose"]),
        "effect": {
            "class": str(item["effect"]["class"]),
            "read_only": bool(item["effect"]["read_only"]),
            "canonical": "none",
            "public": "none",
        },
        "input_schema": deep_thaw(
            _projected_semantic_input_schema(
                operation,
                historical_read=historical_read,
                admitted_result_closeout=admitted_result_closeout,
            )
        ),
        "examples": examples,
        "correction": {
            "usage_call": {
                "operation": MISSION_MODEL_USAGE_OPERATION,
                "input": {"for_operation": operation},
            },
            "instruction": (
                "Retry only if this exact operation is still intended, preserving "
                "the mathematical meaning while matching one published example "
                "and the exact input schema."
            ),
        },
    }


def _project_model_usage(
    allowed_operations: Iterable[str] | None = None,
    *,
    for_operation: str | None = None,
    tool_name: str = "rh_mission",
    historical_read: bool,
    admitted_result_closeout: bool,
) -> Mapping[str, Any]:
    allowed = _subset(allowed_operations)
    tool_name = _validated_tool_name(tool_name)
    if for_operation is not None:
        if for_operation not in allowed:
            raise MissionOperationContractError(
                "mission_operation_not_allowed",
                "$.input.for_operation",
                f"Operation {for_operation!r} is outside the authorized subset.",
            )
        return deep_freeze(
            _model_operation_guide(
                for_operation,
                tool_name=tool_name,
                historical_read=historical_read,
                admitted_result_closeout=admitted_result_closeout,
            )
        )
    return deep_freeze(
        {
            "schema_version": MISSION_MODEL_USAGE_SCHEMA_VERSION,
            "tool": tool_name,
            "status": "ok",
            "purpose": (
                "Use one zero-effect handshake to learn the exact worker-facing RH "
                "Mission command grammar before issuing a semantic owner operation."
            ),
            "command_grammar": '{"operation":"<usage-or-semantic-operation>","input":{...}}',
            "safe_first_calls": [
                {
                    "label": "List the authorized semantic operations",
                    "call": {"operation": MISSION_MODEL_USAGE_OPERATION, "input": {}},
                },
                {
                    "label": "Show one exact operation guide",
                    "call": {
                        "operation": MISSION_MODEL_USAGE_OPERATION,
                        "input": {"for_operation": RETRIEVE},
                    },
                },
            ],
            "side_effects": {
                "writes_mission": False,
                "canonical": "none",
                "public": "none",
                "provider": "none",
                "credential": "none",
            },
            "semantic_operation_count": len(allowed),
            "operations": [
                {
                    "operation": operation,
                    "purpose": str(mission_operation(operation)["purpose"]),
                    "effect_class": str(
                        mission_operation(operation)["effect"]["class"]
                    ),
                    "read_only": bool(
                        mission_operation(operation)["effect"]["read_only"]
                    ),
                    "usage_call": {
                        "operation": MISSION_MODEL_USAGE_OPERATION,
                        "input": {"for_operation": operation},
                    },
                }
                for operation in allowed
            ],
            "errors": [
                {
                    "code": "mission_operation_request_invalid",
                    "meaning": (
                        "The selected semantic input differs from its exact owner "
                        "schema; use the returned targeted guide before retrying."
                    ),
                },
                {
                    "code": "mission_operation_not_allowed",
                    "meaning": "The selected operation is outside this authorized projection.",
                },
            ],
        }
    )


def project_model_usage(
    allowed_operations: Iterable[str] | None = None,
    *,
    for_operation: str | None = None,
    tool_name: str = "rh_mission",
) -> Mapping[str, Any]:
    """Project the zero-effect root-console handshake from the semantic owner."""

    return _project_model_usage(
        allowed_operations,
        for_operation=for_operation,
        tool_name=tool_name,
        historical_read=False,
        admitted_result_closeout=False,
    )


def _project_model_operations(
    allowed_operations: Iterable[str] | None = None,
    *,
    tool_name: str = "rh_mission",
    historical_read: bool,
    admitted_result_closeout: bool,
) -> Mapping[str, Any]:
    allowed = _subset(allowed_operations)
    tool_name = _validated_tool_name(tool_name)
    input_schema = {
        "type": "object",
        "description": (
            "RH Mission console. The zero-effect usage command teaches the exact "
            "input for one authorized semantic operation. Every non-usage operation "
            "executes immediately through the Mission owner."
        ),
        "additionalProperties": False,
        "properties": {
            "operation": {
                "type": "string",
                "enum": [MISSION_MODEL_USAGE_OPERATION, *allowed],
                "description": (
                    "Use usage for safe interface discovery; the remaining values "
                    "are the authorized immediate Mission semantic operations."
                ),
            },
            "input": {
                "type": "object",
                "description": (
                    "For usage pass {} or {\"for_operation\":\"<operation>\"}. "
                    "For a semantic operation use its exact targeted usage guide."
                ),
            },
        },
        "required": ["operation", "input"],
    }
    projection = {
        "schema_version": MISSION_MODEL_PROJECTION_SCHEMA_VERSION,
        "allowed_operations": list(allowed),
        "semantic_request_schema_version": MISSION_SEMANTIC_REQUEST_SCHEMA_VERSION,
        "input_schema": input_schema,
        "usage": {
            "index": deep_thaw(
                _project_model_usage(
                    allowed,
                    tool_name=tool_name,
                    historical_read=historical_read,
                    admitted_result_closeout=admitted_result_closeout,
                )
            ),
            "operation_guides": {
                operation: deep_thaw(
                    _project_model_usage(
                        allowed,
                        for_operation=operation,
                        tool_name=tool_name,
                        historical_read=historical_read,
                        admitted_result_closeout=admitted_result_closeout,
                    )
                )
                for operation in allowed
            },
        },
    }
    return deep_freeze(projection)


def project_model_operations(
    allowed_operations: Iterable[str] | None = None,
    *,
    tool_name: str = "rh_mission",
) -> Mapping[str, Any]:
    """Project the root console; delegated history is projected only by its owner."""

    return _project_model_operations(
        allowed_operations,
        tool_name=tool_name,
        historical_read=False,
        admitted_result_closeout=False,
    )


def project_mission_host_model_projection() -> Mapping[str, Any]:
    """Project the root console and its separate delegated specialist planes."""

    return deep_freeze(
        {
            "schema_version": MISSION_HOST_MODEL_PROJECTION_SCHEMA_VERSION,
            "root_tool": {
                "name": "rh_mission",
                "model_projection": deep_thaw(project_model_operations()),
            },
            "closeout_root_tool": {
                "name": "rh_mission",
                "model_projection": deep_thaw(
                    _project_model_operations(
                        (RECORD_STRATEGY, CHECKPOINT),
                        tool_name="rh_mission",
                        historical_read=False,
                        admitted_result_closeout=True,
                    )
                ),
            },
            "historical_read_tool": {
                "name": "rh_mission_history",
                "model_projection": deep_thaw(
                    _project_model_operations(
                        HISTORICAL_READ_OPERATIONS,
                        tool_name="rh_mission_history",
                        historical_read=True,
                        admitted_result_closeout=False,
                    )
                ),
                "grant_input_schema": deep_thaw(
                    historical_read_grant_input_schema()
                ),
            },
            "research_read_tool": {
                "name": "rh_mission_research_read",
                "grant_tool_name": "rh_mission_research_read_grant",
                "page_tool_name": "rh_mission_research_read_page",
                "grant_input_schema": deep_thaw(research_read_grant_input_schema()),
                "request_schema": deep_thaw(research_read_request_schema()),
            },
            "candidate_a1_review_tool": {
                "name": "rh_mission_a1_review",
                "grant_tool_name": "rh_mission_a1_review_grant",
                "page_tool_name": "rh_mission_a1_review_page",
                "grant_input_schema": deep_thaw(
                    candidate_a1_review_grant_input_schema()
                ),
                "request_schema": deep_thaw(candidate_a1_review_request_schema()),
            },
            "admission_tool": {
                "name": "rh_mission_admission",
                "open_tool_name": "rh_mission_admission_open",
                "grant_tool_name": "rh_mission_admission_grant",
                "page_tool_name": "rh_mission_admission_page",
                "case_input_schema": deep_thaw(admission_case_request_schema()),
                "grant_input_schema": deep_thaw(
                    admission_host_grant_input_schema()
                ),
                "request_schema": deep_thaw(admission_child_request_schema()),
            },
        }
    )


def project_cli_operations(
    allowed_operations: Iterable[str] | None = None,
) -> Mapping[str, Any]:
    allowed = _subset(allowed_operations)
    projection = {
        "schema_version": MISSION_CLI_PROJECTION_SCHEMA_VERSION,
        "allowed_operations": list(allowed),
        "verbs": [
            {
                "verb": mission_operation(operation)["cli_verb"],
                "operation": operation,
                "effect_class": mission_operation(operation)["effect"]["class"],
                "read_only": mission_operation(operation)["effect"]["read_only"],
                "supports_dry_run": not mission_operation(operation)["effect"]["read_only"],
                "execute_method": MISSION_INTERFACE_EXECUTE_METHOD,
                "preview_method": (
                    None
                    if mission_operation(operation)["effect"]["read_only"]
                    else MISSION_INTERFACE_PREVIEW_METHOD
                ),
            }
            for operation in allowed
        ],
    }
    return deep_freeze(projection)


def project_operation_capabilities(
    allowed_operations: Iterable[str] | None = None,
) -> Mapping[str, Any]:
    projection = project_cli_operations(allowed_operations)
    return deep_freeze(
        {
            "activation_state": "noncanonical_runtime",
            "canonical_effect": "none",
            "public_effect": "none",
            "provider_effect": "none",
            "operations": {
                item["operation"]: {
                    key: item[key]
                    for key in (
                        "verb",
                        "effect_class",
                        "read_only",
                        "execute_method",
                        "preview_method",
                    )
                }
                for item in projection["verbs"]
            },
        }
    )


_FORBIDDEN_OWNER_KEYS = frozenset(
    {
        "actor",
        "actor_id",
        "principal",
        "principal_id",
        "command_id",
        "idempotency_key",
        "expected_state",
        "project_commit",
        "root_digest",
        "canonical_authority_digest",
        "mission_id",
        "mission_revision",
        "authority",
        "authority_object",
        "executive_epoch_id",
        "goal_thread_id",
        "thread_id",
        "turn_id",
        "call_id",
        "lease",
        "writer_lease",
        "writer_epoch",
        "issuer",
        "seal",
        "hmac",
        "cas_path",
        "provider",
        "provider_id",
        "model",
        "reasoning_effort",
        "resource_limits",
        "measured_resources",
        "opened_at",
        "issued_at",
        "authored_at",
        "recorded_at",
        "observed_at",
        "effective_at",
    }
)


def _candidate_argument_authority_paths(
    request: Mapping[str, Any],
) -> frozenset[tuple[str | int, ...]]:
    """Locate only the two typed mathematical containers in a root request.

    Shape validation remains with the existing Candidate schema, including its
    synthesis consequence-local selector handling. This is not a schema bypass
    or a recursive exemption for the contents of mathematical authority.
    """
    semantic_input = request.get("input")
    if not isinstance(semantic_input, Mapping):
        return frozenset()
    candidates: list[tuple[tuple[str | int, ...], Mapping[str, Any]]] = []
    if request.get("operation") == RECORD_CANDIDATE:
        candidates.append((("input",), semantic_input))
    elif request.get("operation") == SYNTHESIZE:
        consequences = semantic_input.get("consequences")
        if isinstance(consequences, (list, tuple)):
            for index, consequence in enumerate(consequences):
                if (
                    isinstance(consequence, Mapping)
                    and consequence.get("kind") == "candidate"
                    and isinstance(consequence.get("candidate"), Mapping)
                ):
                    candidates.append((
                        ("input", "consequences", index, "candidate"),
                        consequence["candidate"],
                    ))
    paths: set[tuple[str | int, ...]] = set()
    for parent, candidate in candidates:
        edges = candidate.get("argument_edges")
        if isinstance(edges, (list, tuple)):
            for index, edge in enumerate(edges):
                if isinstance(edge, Mapping):
                    paths.add((*parent, "argument_edges", index, "authority"))
    return frozenset(paths)


def _owner_fact_violation(
    value: Any,
    location: str = "$",
    *,
    mathematical_authority_paths: frozenset[tuple[str | int, ...]] = frozenset(),
    selector_fact_paths: frozenset[tuple[str | int, ...]] = frozenset(),
    _path: tuple[str | int, ...] = (),
) -> tuple[str, str] | None:
    if isinstance(value, Mapping):
        for key, item in value.items():
            normalized = str(key).lower().lstrip("_")
            path = (*_path, key)
            typed_mathematical_authority = (
                key == "authority" and path in mathematical_authority_paths
            )
            if path not in selector_fact_paths and ((
                normalized in _FORBIDDEN_OWNER_KEYS and not typed_mathematical_authority
            ) or normalized.endswith(
                ("_sha256", "_base64", "_byte_length", "_bytes")
            )):
                return f"{location}.{key}", f"{key} is owner-derived"
            nested = _owner_fact_violation(
                item, f"{location}.{key}",
                mathematical_authority_paths=mathematical_authority_paths,
                selector_fact_paths=selector_fact_paths,
                _path=path,
            )
            if nested is not None:
                return nested
    elif isinstance(value, (list, tuple)):
        for index, item in enumerate(value):
            nested = _owner_fact_violation(
                item, f"{location}[{index}]",
                mathematical_authority_paths=mathematical_authority_paths,
                selector_fact_paths=selector_fact_paths,
                _path=(*_path, index),
            )
            if nested is not None:
                return nested
    return None


def _scientific_selector_fact_paths(
    request: Mapping[str, Any],
) -> frozenset[tuple[str | int, ...]]:
    """Permit only typed immutable selectors, never caller-supplied authority.

    Complete scientific Context creates retain explicit owner identities, which
    the Context owner checks against the bound Mission. Digests select exact
    returned references; neither they nor publication lookup identities grant a
    writer, epoch, current-state claim or an arbitrary nested exemption.
    """
    value = request.get("input")
    if not isinstance(value, Mapping):
        return frozenset()
    paths: set[tuple[str | int, ...]] = set()
    if request.get("operation") == RETRIEVE and value.get("mode") == "publication_result":
        return frozenset({("input", "command_id")})
    groups: list[tuple[tuple[str | int, ...], Mapping[str, Any]]] = []
    if request.get("operation") == RECORD_CONTEXT and value.get("mode") == "scientific":
        groups.append((("input",), value))
    elif request.get("operation") == INTERPRET_MATERIAL and isinstance(value.get("scientific_continuation"), Mapping):
        groups.append((("input", "scientific_continuation"), value["scientific_continuation"]))
        paths.add(("input", "expected_head", "payload_sha256"))
    elif request.get("operation") == SYNTHESIZE:
        consequences = value.get("consequences")
        for index, item in enumerate(consequences if isinstance(consequences, (list, tuple)) else ()):
            if isinstance(item, Mapping) and item.get("kind") == "evidence" and isinstance(item.get("scientific_continuation"), Mapping):
                base = ("input", "consequences", index)
                groups.append(((*base, "scientific_continuation"), item["scientific_continuation"]))
                paths.add((*base, "expected_head", "payload_sha256"))
    for base, group in groups:
        updates = group.get("updates")
        if not isinstance(updates, (list, tuple)):
            continue
        for index, update in enumerate(updates):
            if not isinstance(update, Mapping):
                continue
            update_path = (*base, "updates", index)
            paths.add((*update_path, "expected_head", "payload_sha256"))
            treatment_maps: list[tuple[tuple[str | int, ...], Any]] = []
            create = update.get("create")
            if isinstance(create, Mapping):
                paths.add((*update_path, "create", "mission_id"))
                treatment_maps.append(((*update_path, "create", "treatments"), create.get("treatments")))
            patch = update.get("patch")
            if isinstance(patch, Mapping):
                for operation in ("insert", "replace"):
                    treatment_maps.append(((*update_path, "patch", operation), patch.get(operation)))
            for treatment_path, treatments in treatment_maps:
                if not isinstance(treatments, Mapping):
                    continue
                for treatment_id, treatment in treatments.items():
                    # A map key is a local treatment identity, not an owner
                    # field. Its value still receives full recursive checking.
                    paths.add((*treatment_path, treatment_id))
                    if not isinstance(treatment, Mapping) or not isinstance(treatment.get("sources"), (list, tuple)):
                        continue
                    for source_index, source in enumerate(treatment["sources"]):
                        if isinstance(source, Mapping):
                            source_path = (*treatment_path, treatment_id, "sources", source_index)
                            paths.add((*source_path, "reference", "payload_sha256"))
                            paths.add((*source_path, "dependency", "observed_current_reference", "payload_sha256"))
    return frozenset(paths)


def validate_evidence_consequence_input(value: Mapping[str, Any]) -> Mapping[str, Any]:
    """Validate one Evidence consequence inside the independent sibling loop."""
    request = {"operation": SYNTHESIZE, "input": {"consequences": [value]}}
    forbidden = _owner_fact_violation(
        request, selector_fact_paths=_scientific_selector_fact_paths(request),
    )
    if forbidden:
        raise MissionOperationContractError("mission_operation_owner_fact_forbidden", *forbidden)
    schema = deep_thaw(_EVIDENCE_CONSEQUENCE)
    violations = _schema_violations(value, schema, schema, "$.consequence")
    if violations:
        raise MissionOperationContractError("mission_operation_request_invalid", *violations[0])
    if ("scientific_continuation" in value) != ("expected_head" in value):
        raise MissionOperationContractError(
            "mission_operation_request_invalid", "$.consequence",
            "scientific_continuation and its exact expected_head must be supplied together",
        )
    return deep_freeze(deep_thaw(value))


def _validate_semantic_request(
    value: Mapping[str, Any],
    *,
    allowed_operations: Iterable[str] | None = None,
    historical_read: bool,
) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise MissionOperationContractError(
            "mission_operation_request_invalid", "$", "Request must be one object."
        )
    fact_input = value
    if value.get("operation") == SYNTHESIZE and isinstance(value.get("input"), Mapping):
        # The writer validates these two paired-group containers inside the
        # existing per-consequence try. A malformed group must not suppress an
        # independent complete-target Candidate before that loop even starts.
        fact_input = deep_thaw(value)
        consequences = fact_input["input"].get("consequences")
        if isinstance(consequences, list):
            for consequence in consequences:
                if isinstance(consequence, dict) and consequence.get("kind") == "evidence":
                    consequence.pop("scientific_continuation", None)
                    consequence.pop("expected_head", None)
    forbidden = _owner_fact_violation(
        fact_input, mathematical_authority_paths=_candidate_argument_authority_paths(value),
        selector_fact_paths=_scientific_selector_fact_paths(value),
    )
    if forbidden:
        raise MissionOperationContractError(
            "mission_operation_owner_fact_forbidden", forbidden[0], forbidden[1]
        )
    allowed = _subset(allowed_operations)
    operation = value.get("operation")
    if operation not in allowed:
        raise MissionOperationContractError(
            "mission_operation_not_allowed",
            "$.operation",
            f"Operation {operation!r} is outside the authorized subset.",
        )
    # The shared validator evaluates ordinary JSON containers. Contract
    # projections are deeply immutable, so thaw the schema before evaluation;
    # otherwise tuple-backed ``required``/``oneOf`` vocabulary is skipped.
    schema = deep_thaw(
        _semantic_request_schema(
            str(operation),
            historical_read=historical_read,
        )
    )
    if operation == SYNTHESIZE:
        evidence = schema["properties"]["input"]["properties"]["consequences"]["items"]["oneOf"][0]
        evidence["properties"]["scientific_continuation"] = {}
        evidence["properties"]["expected_head"] = {}
    violations = _schema_violations(value, schema, schema, "$")
    if violations:
        location, message = violations[0]
        raise MissionOperationContractError(
            "mission_operation_request_invalid", location, message
        )
    if operation == INTERPRET_MATERIAL:
        semantic_input = value["input"]
        if ("scientific_continuation" in semantic_input) != ("expected_head" in semantic_input):
            raise MissionOperationContractError(
                "mission_operation_request_invalid", "$.input",
                "scientific_continuation and its exact expected_head must be supplied together",
            )
    return deep_freeze(deep_thaw(value))


def validate_semantic_request(
    value: Mapping[str, Any],
    *,
    allowed_operations: Iterable[str] | None = None,
) -> Mapping[str, Any]:
    """Validate one root-console request; history modes require the alias grant."""

    return _validate_semantic_request(
        value,
        allowed_operations=allowed_operations,
        historical_read=False,
    )


def validate_historical_read_request(
    value: Mapping[str, Any],
) -> Mapping[str, Any]:
    """Validate one granted ``rh_mission_history`` orient/retrieve request."""

    return _validate_semantic_request(
        value,
        allowed_operations=HISTORICAL_READ_OPERATIONS,
        historical_read=True,
    )


def parse_semantic_request_bytes(
    raw: bytes,
    *,
    allowed_operations: Iterable[str] | None = None,
) -> Mapping[str, Any]:
    try:
        value = loads_strict_json_object(raw)
    except (TypeError, UnicodeError, ValueError) as exc:
        raise MissionOperationContractError(
            "mission_operation_request_invalid", "$", str(exc)
        ) from exc
    return validate_semantic_request(value, allowed_operations=allowed_operations)


def validate_semantic_result(operation: str, value: Mapping[str, Any]) -> Mapping[str, Any]:
    mission_operation(operation)
    schema = deep_thaw(semantic_result_schema(operation))
    violations = _schema_violations(value, schema, schema, "$")
    if violations:
        location, message = violations[0]
        raise MissionOperationContractError(
            "mission_operation_result_invalid", location, message
        )
    if operation in {ORIENT, RETRIEVE} and value.get("status") == "completed" and value.get("result", {}).get("view") != "historical":
        # Closed read-side schemas own the exact effectful A1/Admission/formal
        # selectors and explicit semantic projections. Do not recursively treat
        # authored mathematical JSON as model-authored authority. Request/write
        # validation and the separate delegated history lane remain unchanged.
        if operation == ORIENT:
            validate_executive_orientation(value["result"]["orientation"])
        return deep_freeze(deep_thaw(value))
    owner_fact_view = deep_thaw(value)
    if owner_fact_view.get("status") == "completed":
        result = owner_fact_view["result"]
        # The closed publication result binds authenticated original effects.
        # These digest-bearing exact selectors are not raw custody encodings.
        if operation == RECORD_CONTEXT and result.get("schema_version") == "mathematical_research.scientific_publication_result.v1":
            owner_fact_view["result"] = {}
        elif operation in {INTERPRET_MATERIAL, SYNTHESIZE}:
            result.pop("publication", None)
            for record in result.get("records", ()):
                record.pop("publication", None)
    if operation == RECORD_STRATEGY and owner_fact_view.get("status") == "completed":
        # This one digest is an owner-derived selector for the separate formal
        # execution capability. It is not model-authored Strategy meaning or
        # custody accounting. Keep the generic digest prohibition intact for
        # every other result location and for every semantic request.
        for item in owner_fact_view["result"]["formal_requests"]:
            item.pop("selected_bet_sha256")
    if operation == RECORD_CANDIDATE and owner_fact_view.get("status") == "completed":
        # An OPEN A1 result must name the exact Candidate revision held for
        # review.  Its digest is an owner-derived retrieval selector, not
        # model-authored proof material or general custody accounting.
        open_candidate_a1 = owner_fact_view["result"]["open_candidate_a1"]
        if open_candidate_a1 is not None:
            open_candidate_a1["candidate_ref"].pop("payload_sha256")
    forbidden = _owner_fact_violation(owner_fact_view)
    if forbidden and forbidden[0].endswith(("_base64", "_sha256", "_byte_length")):
        raise MissionOperationContractError(
            "mission_operation_result_invalid",
            forbidden[0],
            "Readable semantic results cannot expose encodings or custody accounting.",
        )
    error = value.get("error")
    if isinstance(error, Mapping):
        metadata = {
            item["code"]: item for item in mission_operation(operation)["errors"]
        }.get(error.get("code"))
        if metadata is None or any(
            error.get(key) != metadata[key]
            for key in ("property", "failure_scope", "correction")
        ):
            raise MissionOperationContractError(
                "mission_operation_result_invalid",
                "$.error",
                "Error metadata differs from the canonical property meaning.",
            )
    return deep_freeze(deep_thaw(value))


__all__ = [
    "ADMISSION_DECISION_DISPOSITIONS",
    "ADMISSION_DECISION_MODES",
    "ADMISSION_REVIEW_DISPOSITIONS",
    "ADMISSION_REVIEW_MODES",
    "ADMISSION_HOST_GRANT_ROLES",
    "CANDIDATE_A1_REVIEW_DISPOSITIONS",
    "CANDIDATE_A1_REVIEW_MODES",
    "CHECKPOINT",
    "HISTORICAL_ASSIGNMENT_MODES",
    "HISTORICAL_RAW_BODY_POLICIES",
    "HISTORICAL_READ_OPERATIONS",
    "HISTORICAL_RELATIONSHIP_KINDS",
    "HISTORICAL_RETRIEVE_MODES",
    "HISTORICAL_SEARCH_FIELDS",
    "HISTORICAL_SOURCE_FAMILIES",
    "INTERPRET_MATERIAL",
    "MISSION_INTERFACE_EXECUTE_METHOD",
    "MISSION_INTERFACE_PREVIEW_METHOD",
    "MISSION_OPERATION_CONTRACT_SCHEMA_VERSION",
    "MISSION_MODEL_PROJECTION_SCHEMA_VERSION",
    "MISSION_MODEL_USAGE_OPERATION",
    "MISSION_MODEL_USAGE_SCHEMA_VERSION",
    "MISSION_HOST_MODEL_PROJECTION_SCHEMA_VERSION",
    "MISSION_OPERATION_ORDER",
    "MISSION_SEMANTIC_REQUEST_SCHEMA_VERSION",
    "MISSION_SEMANTIC_RESULT_SCHEMA_VERSION",
    "MissionOperationContractError",
    "ORIENT",
    "RECORD_BRANCH",
    "RECORD_CANDIDATE",
    "RECORD_CONTEXT",
    "RECORD_STRATEGY",
    "RETRIEVE",
    "RESEARCH_READ_MODES",
    "RESEARCH_SOURCE_FAMILIES",
    "RESEARCH_READ_USAGE_TOPICS",
    "SYNTHESIZE",
    "mission_operation",
    "mission_operation_contract",
    "mission_operation_ids",
    "operation_from_cli_verb",
    "parse_semantic_request_bytes",
    "project_cli_operations",
    "project_model_operations",
    "project_mission_host_model_projection",
    "project_model_usage",
    "project_operation_capabilities",
    "semantic_request_schema",
    "semantic_result_schema",
    "admission_case_request_schema",
    "admission_child_request_schema",
    "admission_decision_request_schema",
    "admission_host_grant_input_schema",
    "admission_review_request_schema",
    "candidate_a1_review_grant_input_schema",
    "candidate_a1_review_request_schema",
    "historical_read_grant_input_schema",
    "research_read_grant_input_schema",
    "research_read_selection_schema",
    "research_read_request_schema",
    "research_read_result_schema",
    "validate_research_read_grant_request",
    "validate_research_read_request",
    "validate_research_read_result",
    "validate_candidate_a1_review_grant_request",
    "validate_candidate_a1_review_request",
    "validate_admission_case_request",
    "validate_admission_decision_grant_request",
    "validate_admission_decision_request",
    "validate_admission_review_grant_request",
    "validate_admission_review_request",
    "validate_historical_read_request",
    "validate_historical_read_grant_request",
    "validate_semantic_request",
    "validate_evidence_consequence_input",
    "validate_semantic_result",
]
