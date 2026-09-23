"""Direct first-class Context revision owner.

Context meaning is an exact closed semantic document. Live publication belongs
to the current direct Executive Epoch; explicitly authorized stopped maintenance
belongs to its same-principal Store owner. Pure preparation grants neither role.
Immutable content, digest and owner-scoped guards make the value self-validating
without a private issuer seal. Scientific reference roles remain distinct from
publication-time guards.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from typing import Any, Mapping, Sequence

from .json_support import canonical_json_bytes
from .mission_executive import DirectExecutiveEpochAuthority
from .research_model import deep_freeze, deep_thaw


CONTEXT_REVISION_SCHEMA_VERSION = 1
CONTEXT_REVISION_SCHEMA_VERSION_V2 = 2
CONTEXT_REVISION_SCHEMA_VERSION_V3 = 3

HISTORICAL_ADVISORY_ASSIGNMENT_MODES = (
    "historical_opportunity_scout",
    "lifecycle_historian",
)
HISTORICAL_ADVISORY_SOURCE_FAMILIES = (
    "branches",
    "candidates",
    "strategies",
    "contexts",
    "evidence",
    "capture_annotations",
    "captures",
    "capture_artifacts",
)

_SHA256_RE = re.compile(r"[0-9a-f]{64}")
_REFERENCE_KEYS = {"kind", "identity", "revision", "payload_sha256"}
_CONTEXT_REVISION_KEYS = {
    "schema_version",
    "kind",
    "project_id",
    "mission_id",
    "context_id",
    "purpose",
    "question",
    "indispensable_ground",
    "owner_source_references",
    "known_omissions",
    "restricted_uses",
    "independence_treatment",
    "restrictions",
    "invalidation_conditions",
}
_CONTEXT_REVISION_V2_KEYS = _CONTEXT_REVISION_KEYS | {
    "immutable_historical_references",
    "untrusted_material_locators",
    "historical_advisory_scope",
}
_CONTEXT_REVISION_V3_KEYS = {
    "schema_version", "kind", "project_id", "mission_id", "context_id",
    "purpose", "question", "treatments", "exposed_treatments",
    "known_omissions", "restricted_uses", "restrictions", "independence_treatment",
}
_SCIENTIFIC_TREATMENT_KEYS = {"question", "account", "qualifications", "sources"}
_SCIENTIFIC_SOURCE_KEYS = {"reference", "selection", "roles", "why", "dependency"}
_SCIENTIFIC_DEPENDENCY_KEYS = {
    "observed_current_reference", "change_that_matters", "dependent_judgment",
}
_SCIENTIFIC_METADATA_KEYS = {
    "purpose", "question", "known_omissions", "restricted_uses", "restrictions",
    "independence_treatment",
}
_SCIENTIFIC_PATCH_KEYS = {
    "insert", "replace", "remove", "exposure_add", "exposure_remove", "metadata_replace",
}
SCIENTIFIC_SOURCE_ROLES = ("history", "recognition", "reliance")
_CONTEXT_GROUND_KEYS = {"reference", "why"}
_CONTEXT_SOURCE_KEYS = {"reference", "retrieval", "provenance"}
_CONTEXT_INVALIDATION_KEYS = {"reference", "condition"}
_CONTEXT_HISTORICAL_REFERENCE_KEYS = {"reference", "why"}
_CONTEXT_UNTRUSTED_LOCATOR_KEYS = {"id", "why", "treatment"}
_CONTEXT_HISTORICAL_SCOPE_KEYS = {
    "assignment_mode",
    "search_lens",
    "source_families",
    "known_omissions",
    "coverage_limits",
}


def _sha256(value: Any) -> str:
    raw = value if isinstance(value, bytes) else canonical_json_bytes(value)
    return hashlib.sha256(raw).hexdigest()


def _require_text(value: Any, location: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{location} must be a non-empty string")
    return value


def _require_object(value: Any, location: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping) or not value:
        raise ValueError(f"{location} must be a non-empty object")
    return value


def _require_positive_int(value: Any, location: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ValueError(f"{location} must be a positive integer")
    return value


def _require_sha256(value: Any, location: str) -> str:
    if not isinstance(value, str) or _SHA256_RE.fullmatch(value) is None:
        raise ValueError(f"{location} must be a lowercase SHA-256")
    return value


def _require_mapping_sequence(
    value: Any,
    location: str,
) -> tuple[Mapping[str, Any], ...]:
    if not isinstance(value, Sequence) or isinstance(
        value,
        (str, bytes, bytearray),
    ):
        raise ValueError(f"{location} must be an array")
    result: list[Mapping[str, Any]] = []
    for index, item in enumerate(value):
        if not isinstance(item, Mapping):
            raise ValueError(f"{location}[{index}] must be an object")
        result.append(item)
    return tuple(result)


def _require_text_sequence(value: Any, location: str) -> tuple[str, ...]:
    if not isinstance(value, Sequence) or isinstance(
        value,
        (str, bytes, bytearray),
    ):
        raise ValueError(f"{location} must be an array")
    result = tuple(
        _require_text(item, f"{location}[{index}]") for index, item in enumerate(value)
    )
    if result != tuple(sorted(result)) or len(result) != len(set(result)):
        raise ValueError(f"{location} must be unique and sorted")
    return result


def context_reference_key(
    reference: Mapping[str, Any],
) -> tuple[str, str, int, str]:
    """Return the complete immutable Context-reference identity."""

    if not isinstance(reference, Mapping) or set(reference) != _REFERENCE_KEYS:
        raise ValueError(
            "Context reference must have kind, identity, revision, and digest"
        )
    return (
        _require_text(reference["kind"], "reference.kind"),
        _require_text(reference["identity"], "reference.identity"),
        _require_positive_int(reference["revision"], "reference.revision"),
        _require_sha256(reference["payload_sha256"], "reference.payload_sha256"),
    )


def validate_context_source_selection(
    reference: Mapping[str, Any], selection: Any,
) -> None:
    """Validate explicit treatment/whole-Context use, never a current selector."""

    kind, _, _, _ = context_reference_key(reference)
    if kind != "context":
        if selection is not None:
            raise ValueError("only a Context source can have a treatment selection")
        return
    if not isinstance(selection, Mapping):
        raise ValueError("a Context source requires an explicit selection")
    if selection.get("mode") == "whole_context":
        if set(selection) != {"mode"}:
            raise ValueError("whole-Context selection has the wrong closed shape")
    elif selection.get("mode") == "treatments":
        if set(selection) != {"mode", "treatment_ids"}:
            raise ValueError("treatment selection has the wrong closed shape")
        if not _require_text_sequence(selection["treatment_ids"], "treatment_ids"):
            raise ValueError("treatment selection must not be empty")
    else:
        raise ValueError("Context source selection mode is unsupported")


def _validate_scientific_treatment(value: Any, location: str) -> None:
    if not isinstance(value, Mapping) or set(value) != _SCIENTIFIC_TREATMENT_KEYS:
        raise ValueError(f"{location} has the wrong closed shape")
    for key in ("question", "account"):
        _require_text(value[key], f"{location}.{key}")
    qualifications = value["qualifications"]
    if not isinstance(qualifications, (list, tuple)):
        raise ValueError(f"{location}.qualifications must be an array")
    for qualification in qualifications:
        _require_text(qualification, f"{location}.qualifications")
    seen: set[bytes] = set()
    for source in _require_mapping_sequence(value["sources"], f"{location}.sources"):
        if set(source) != _SCIENTIFIC_SOURCE_KEYS:
            raise ValueError(f"{location}.sources has the wrong closed shape")
        reference = source["reference"]
        reference_key = context_reference_key(reference)
        validate_context_source_selection(reference, source["selection"])
        source_key = canonical_json_bytes((reference, source["selection"]))
        if source_key in seen:
            raise ValueError("a treatment must not repeat an exact selected source")
        seen.add(source_key)
        roles = _require_text_sequence(source["roles"], "source.roles")
        if not roles or any(role not in SCIENTIFIC_SOURCE_ROLES for role in roles):
            raise ValueError("scientific source roles are unsupported or empty")
        _require_text(source["why"], "source.why")
        dependency = source["dependency"]
        if dependency is None:
            continue
        if "reliance" not in roles:
            raise ValueError("only reliance permits a current-head dependency")
        if not isinstance(dependency, Mapping) or set(dependency) != _SCIENTIFIC_DEPENDENCY_KEYS:
            raise ValueError("scientific dependency has the wrong closed shape")
        observed = context_reference_key(dependency["observed_current_reference"])
        if observed[:2] != reference_key[:2]:
            raise ValueError("observed current head must retain the cited source identity")
        _require_text(dependency["change_that_matters"], "dependency.change_that_matters")
        _require_text(dependency["dependent_judgment"], "dependency.dependent_judgment")


def _exposure_key(value: Mapping[str, Any], *, removal: bool = False) -> tuple[str, str]:
    keys = {"context_id", "treatment_id", "reason"} if removal else {"context_id", "treatment_id"}
    if not isinstance(value, Mapping) or set(value) != keys:
        raise ValueError("scientific exposure selection has the wrong closed shape")
    context_id = _require_text(value["context_id"], "exposure.context_id")
    if context_id.startswith("context:"):
        raise ValueError("persisted exposure Context identity must be bare")
    treatment_id = _require_text(value["treatment_id"], "exposure.treatment_id")
    if removal:
        _require_text(value["reason"], "exposure removal reason")
    return context_id, treatment_id


def _validate_scientific_context_document(document: Mapping[str, Any]) -> None:
    if str(document["context_id"]).startswith("context:"):
        raise ValueError("persisted scientific Context identity must be bare")
    treatments = document["treatments"]
    if not isinstance(treatments, Mapping):
        raise ValueError("scientific treatments must be an object")
    for treatment_id, treatment in treatments.items():
        _require_text(treatment_id, "treatment_id")
        _validate_scientific_treatment(treatment, f"treatments.{treatment_id}")
    seen: set[tuple[str, str]] = set()
    for value in _require_mapping_sequence(document["exposed_treatments"], "exposed_treatments"):
        key = _exposure_key(value)
        if key[0] == document["context_id"]:
            raise ValueError("root-local treatments cannot be explicitly self-selected")
        if key in seen:
            raise ValueError("exposed_treatments must not repeat a selection")
        seen.add(key)
    for key in ("known_omissions", "restricted_uses", "restrictions"):
        _require_text_sequence(document[key], key)
    canonical_json_bytes(_require_object(document["independence_treatment"], "independence_treatment"))


def _validate_first_class_context_document(document: Mapping[str, Any]) -> None:
    if not isinstance(document, Mapping):
        raise ValueError("first-class Context has the wrong closed shape")
    schema_version = document.get("schema_version")
    expected_keys = (
        _CONTEXT_REVISION_KEYS
        if schema_version == CONTEXT_REVISION_SCHEMA_VERSION
        else _CONTEXT_REVISION_V2_KEYS
        if schema_version == CONTEXT_REVISION_SCHEMA_VERSION_V2
        else _CONTEXT_REVISION_V3_KEYS
        if schema_version == CONTEXT_REVISION_SCHEMA_VERSION_V3
        else None
    )
    if expected_keys is None:
        raise ValueError("first-class Context schema version is unsupported")
    if set(document) != expected_keys:
        raise ValueError("first-class Context has the wrong closed shape")
    if document["kind"] != "first_class_context":
        raise ValueError("first-class Context kind is invalid")
    for key in ("project_id", "mission_id", "context_id", "purpose", "question"):
        _require_text(document[key], key)
    if schema_version == CONTEXT_REVISION_SCHEMA_VERSION_V3:
        _validate_scientific_context_document(document)
        return

    sources = _require_mapping_sequence(
        document["owner_source_references"],
        "owner_source_references",
    )
    source_keys: set[tuple[str, str, int, str]] = set()
    for index, item in enumerate(sources):
        if set(item) != _CONTEXT_SOURCE_KEYS:
            raise ValueError(
                f"owner_source_references[{index}] has the wrong closed shape"
            )
        reference_key = context_reference_key(item["reference"])
        if reference_key in source_keys:
            raise ValueError("owner_source_references must not repeat a reference")
        source_keys.add(reference_key)
        _require_text(
            item["retrieval"],
            f"owner_source_references[{index}].retrieval",
        )
        _require_text(
            item["provenance"],
            f"owner_source_references[{index}].provenance",
        )

    ground = _require_mapping_sequence(
        document["indispensable_ground"],
        "indispensable_ground",
    )
    if not ground:
        raise ValueError("indispensable_ground must not be empty")
    ground_keys: set[tuple[str, str, int, str]] = set()
    for index, item in enumerate(ground):
        if set(item) != _CONTEXT_GROUND_KEYS:
            raise ValueError(
                f"indispensable_ground[{index}] has the wrong closed shape"
            )
        reference_key = context_reference_key(item["reference"])
        if reference_key not in source_keys:
            raise ValueError(
                "indispensable ground must name an exact owner/source reference"
            )
        if reference_key in ground_keys:
            raise ValueError("indispensable_ground must not repeat a reference")
        ground_keys.add(reference_key)
        _require_text(item["why"], f"indispensable_ground[{index}].why")

    for key in ("known_omissions", "restricted_uses", "restrictions"):
        _require_text_sequence(document[key], key)
    independence = _require_object(
        document["independence_treatment"],
        "independence_treatment",
    )
    canonical_json_bytes(independence)

    invalidations = _require_mapping_sequence(
        document["invalidation_conditions"],
        "invalidation_conditions",
    )
    invalidation_keys: set[tuple[str, str, int, str]] = set()
    for index, item in enumerate(invalidations):
        if set(item) != _CONTEXT_INVALIDATION_KEYS:
            raise ValueError(
                f"invalidation_conditions[{index}] has the wrong closed shape"
            )
        reference_key = context_reference_key(item["reference"])
        if reference_key not in source_keys:
            raise ValueError(
                "invalidation conditions must name an exact owner/source reference"
            )
        if reference_key in invalidation_keys:
            raise ValueError("invalidation_conditions must not repeat a reference")
        invalidation_keys.add(reference_key)
        _require_text(
            item["condition"],
            f"invalidation_conditions[{index}].condition",
        )

    if schema_version == CONTEXT_REVISION_SCHEMA_VERSION_V2:
        historical = _require_mapping_sequence(
            document["immutable_historical_references"],
            "immutable_historical_references",
        )
        historical_keys: set[tuple[str, str, int, str]] = set()
        for index, item in enumerate(historical):
            if set(item) != _CONTEXT_HISTORICAL_REFERENCE_KEYS:
                raise ValueError(
                    f"immutable_historical_references[{index}] has the wrong closed shape"
                )
            reference_key = context_reference_key(item["reference"])
            if reference_key in historical_keys:
                raise ValueError(
                    "immutable_historical_references must not repeat a reference"
                )
            historical_keys.add(reference_key)
            _require_text(
                item["why"],
                f"immutable_historical_references[{index}].why",
            )

        locators = _require_mapping_sequence(
            document["untrusted_material_locators"],
            "untrusted_material_locators",
        )
        locator_ids: set[str] = set()
        for index, item in enumerate(locators):
            if set(item) != _CONTEXT_UNTRUSTED_LOCATOR_KEYS:
                raise ValueError(
                    f"untrusted_material_locators[{index}] has the wrong closed shape"
                )
            locator_id = _require_text(
                item["id"], f"untrusted_material_locators[{index}].id"
            )
            if locator_id in locator_ids:
                raise ValueError("untrusted_material_locators must not repeat an id")
            locator_ids.add(locator_id)
            _require_text(
                item["why"], f"untrusted_material_locators[{index}].why"
            )
            if item["treatment"] != "untrusted_mathematical_material":
                raise ValueError(
                    "untrusted material locator treatment must be "
                    "untrusted_mathematical_material"
                )

        scope = document["historical_advisory_scope"]
        if not isinstance(scope, Mapping) or set(scope) != _CONTEXT_HISTORICAL_SCOPE_KEYS:
            raise ValueError("historical_advisory_scope has the wrong closed shape")
        if scope["assignment_mode"] not in HISTORICAL_ADVISORY_ASSIGNMENT_MODES:
            raise ValueError("historical_advisory_scope assignment_mode is unsupported")
        _require_text(scope["search_lens"], "historical_advisory_scope.search_lens")
        families = _require_text_sequence(
            scope["source_families"],
            "historical_advisory_scope.source_families",
        )
        if not families:
            raise ValueError("historical_advisory_scope.source_families must not be empty")
        if any(item not in HISTORICAL_ADVISORY_SOURCE_FAMILIES for item in families):
            raise ValueError("historical_advisory_scope source family is unsupported")
        _require_text_sequence(
            scope["known_omissions"],
            "historical_advisory_scope.known_omissions",
        )
        _require_text_sequence(
            scope["coverage_limits"],
            "historical_advisory_scope.coverage_limits",
        )


def _context_dependency_heads(
    document: Mapping[str, Any],
) -> tuple[Mapping[str, Any], ...]:
    if document.get("schema_version") == CONTEXT_REVISION_SCHEMA_VERSION_V3:
        # A revision's retained sources are not an all-current-head assertion.
        # The scientific publication derives guards from changed treatments.
        return ()
    references = {
        context_reference_key(item["reference"]): deep_freeze(item["reference"])
        for item in document["owner_source_references"]
    }
    return tuple(references[key] for key in sorted(references))


def context_reference_projection(
    document: Mapping[str, Any],
    *,
    purpose: str,
    treatment_ids: Sequence[str] | None = None,
    whole_context: bool = False,
) -> tuple[Mapping[str, Any], ...]:
    """Project exact relationships for one consumer, retaining Context selection.

    This does not authorize reads, resolve current heads, or recurse. Retention
    includes concurrency observations; discovery does not. Mathematical-basis
    use of v3 must select treatments explicitly (or author whole-Context use)
    and follows reliance only. An observed head is never substituted for the
    exact historical premise. Exposure membership is current-view navigation,
    not an immutable reference and not a mathematical dependency.
    """

    _validate_first_class_context_document(document)
    if purpose not in {"retention", "discovery", "mathematical_basis", "concurrency"}:
        raise ValueError("Context reference projection purpose is unsupported")
    if not isinstance(whole_context, bool):
        raise TypeError("whole_context must be boolean")
    if whole_context and treatment_ids is not None:
        raise ValueError("select treatments or whole Context, not both")
    projected: dict[bytes, Mapping[str, Any]] = {}

    def add(reference: Mapping[str, Any], selection: Any = None) -> None:
        context_reference_key(reference)
        edge = {"reference": deep_thaw(reference), "selection": deep_thaw(selection)}
        projected[canonical_json_bytes(edge)] = deep_freeze(edge)

    if purpose == "retention":
        # The existing independence field is an open JSON object. Preserve any
        # exact identities it actually carries for custody/audit, without
        # turning anti-anchoring metadata into scientific traversal or proof.
        def retain_independence_references(value: Any) -> None:
            if isinstance(value, Mapping):
                if set(value) == _REFERENCE_KEYS:
                    add(value)
                else:
                    for nested in value.values():
                        retain_independence_references(nested)
            elif isinstance(value, (list, tuple)):
                for nested in value:
                    retain_independence_references(nested)

        retain_independence_references(document["independence_treatment"])

    if document["schema_version"] != CONTEXT_REVISION_SCHEMA_VERSION_V3:
        if treatment_ids is not None:
            raise ValueError("legacy Context has no scientific treatment selection")
        # Keep the established legacy ground/basis and mutable guard semantics.
        for source in document["owner_source_references"]:
            reference = source["reference"]
            selection = {"mode": "whole_context"} if reference["kind"] == "context" else None
            add(reference, selection)
        if purpose in {"retention", "discovery"}:
            for source in document.get("immutable_historical_references", ()):
                reference = source["reference"]
                selection = {"mode": "whole_context"} if reference["kind"] == "context" else None
                add(reference, selection)
        return tuple(projected[key] for key in sorted(projected))

    treatments = document["treatments"]
    if treatment_ids is None:
        if purpose == "mathematical_basis" and not whole_context:
            raise ValueError("scientific mathematical basis requires explicit treatment selection")
        selected = tuple(sorted(treatments))
    else:
        if not isinstance(treatment_ids, (list, tuple)):
            raise ValueError("treatment_ids must be an array")
        selected = tuple(_require_text(value, "treatment_ids") for value in treatment_ids)
        if purpose == "mathematical_basis" and not selected:
            raise ValueError("scientific mathematical basis treatment selection must not be empty")
        if len(selected) != len(set(selected)):
            raise ValueError("treatment_ids must not repeat a treatment")
        if any(value not in treatments for value in selected):
            raise ValueError("selected scientific treatment is unavailable")
    for treatment_id in selected:
        for source in treatments[treatment_id]["sources"]:
            if purpose != "concurrency" and (
                purpose != "mathematical_basis" or "reliance" in source["roles"]
            ):
                add(source["reference"], source["selection"])
            dependency = source["dependency"]
            if purpose in {"retention", "concurrency"} and dependency is not None:
                # A guard has no selected mathematical body, even for Context.
                add(dependency["observed_current_reference"])
    return tuple(projected[key] for key in sorted(projected))


@dataclass(frozen=True, slots=True)
class ContextRevisionRecord:
    """Self-validating semantic Context independent of rendering state."""

    document: Mapping[str, Any]
    digest_sha256: str
    dependency_heads: tuple[Mapping[str, Any], ...]

    def __post_init__(self) -> None:
        object.__setattr__(self, "document", deep_freeze(self.document))
        object.__setattr__(
            self,
            "dependency_heads",
            tuple(deep_freeze(item) for item in self.dependency_heads),
        )
        _validate_first_class_context_document(self.document)
        if self.digest_sha256 != _sha256(self.document):
            raise ValueError("Context revision digest is stale")
        if self.dependency_heads != _context_dependency_heads(self.document):
            raise ValueError("Context revision dependency heads are stale")

    def verify_issued(self) -> None:
        """Validate the exact record value retained by the public contract."""

        if type(self) is not ContextRevisionRecord:
            raise ValueError("Context revision subclasses have no authority")
        self.__post_init__()


@dataclass(frozen=True, slots=True)
class PersistedContextRevision:
    """One Store-owned first-class Context revision and its owner metadata."""

    record: ContextRevisionRecord
    revision: int
    payload_digest: str
    predecessor_revision: int | None
    created_actor: str
    created_at: str
    is_current_head: bool

    def __post_init__(self) -> None:
        if type(self.record) is not ContextRevisionRecord:
            raise TypeError("persisted Context requires an exact ContextRevisionRecord")
        self.record.verify_issued()
        _require_positive_int(self.revision, "Context revision")
        if self.predecessor_revision is not None:
            _require_positive_int(
                self.predecessor_revision,
                "Context predecessor revision",
            )
            if self.predecessor_revision != self.revision - 1:
                raise ValueError("Context predecessor revision is not contiguous")
        elif self.revision != 1:
            raise ValueError("only the first Context revision may lack a predecessor")
        if _require_sha256(self.payload_digest, "Context payload digest") != (
            self.record.digest_sha256
        ):
            raise ValueError("persisted Context payload digest is stale")
        _require_text(self.created_actor, "Context created_actor")
        _require_text(self.created_at, "Context created_at")
        if not isinstance(self.is_current_head, bool):
            raise TypeError("Context current-head state must be boolean")


def _record_from_document(document: Mapping[str, Any]) -> ContextRevisionRecord:
    frozen_document = deep_freeze(deep_thaw(document))
    return ContextRevisionRecord(
        document=frozen_document,
        digest_sha256=_sha256(frozen_document),
        dependency_heads=_context_dependency_heads(frozen_document),
    )


def _require_direct_authority(
    authority: DirectExecutiveEpochAuthority,
    *,
    action: str,
) -> DirectExecutiveEpochAuthority:
    if type(authority) is not DirectExecutiveEpochAuthority:
        raise TypeError(f"{action} requires direct Executive Epoch authority")
    authority.verify_integrity()
    return authority


def issue_context_revision(
    *,
    authority: DirectExecutiveEpochAuthority,
    context_id: str,
    purpose: str,
    question: str,
    indispensable_ground: Sequence[Mapping[str, Any]],
    owner_source_references: Sequence[Mapping[str, Any]],
    known_omissions: Sequence[str],
    restricted_uses: Sequence[str],
    independence_treatment: Mapping[str, Any],
    restrictions: Sequence[str],
    invalidation_conditions: Sequence[Mapping[str, Any]],
) -> ContextRevisionRecord:
    """Issue one first-class Context meaning under the current direct Epoch."""

    authority = _require_direct_authority(
        authority,
        action="first-class Context",
    )

    def ordered_mapping_sequence(
        values: Sequence[Mapping[str, Any]],
        label: str,
    ) -> list[Mapping[str, Any]]:
        items = _require_mapping_sequence(values, label)
        return [deep_thaw(item) for item in sorted(items, key=canonical_json_bytes)]

    def ordered_text_sequence(values: Sequence[str], label: str) -> list[str]:
        if isinstance(values, (str, bytes, bytearray)):
            raise ValueError(f"{label} must be an array")
        result = tuple(_require_text(item, label) for item in values)
        if len(result) != len(set(result)):
            raise ValueError(f"{label} must not contain duplicates")
        return sorted(result)

    document = {
        "schema_version": CONTEXT_REVISION_SCHEMA_VERSION,
        "kind": "first_class_context",
        "project_id": authority.project_id,
        "mission_id": authority.mission_id,
        "context_id": _require_text(context_id, "context_id"),
        "purpose": _require_text(purpose, "purpose"),
        "question": _require_text(question, "question"),
        "indispensable_ground": ordered_mapping_sequence(
            indispensable_ground,
            "indispensable_ground",
        ),
        "owner_source_references": ordered_mapping_sequence(
            owner_source_references,
            "owner_source_references",
        ),
        "known_omissions": ordered_text_sequence(
            known_omissions,
            "known_omissions",
        ),
        "restricted_uses": ordered_text_sequence(
            restricted_uses,
            "restricted_uses",
        ),
        "independence_treatment": deep_thaw(
            _require_object(
                independence_treatment,
                "independence_treatment",
            )
        ),
        "restrictions": ordered_text_sequence(restrictions, "restrictions"),
        "invalidation_conditions": ordered_mapping_sequence(
            invalidation_conditions,
            "invalidation_conditions",
        ),
    }
    return _record_from_document(document)


def issue_context_revision_v2(
    *,
    authority: DirectExecutiveEpochAuthority,
    context_id: str,
    purpose: str,
    question: str,
    indispensable_ground: Sequence[Mapping[str, Any]],
    owner_source_references: Sequence[Mapping[str, Any]],
    known_omissions: Sequence[str],
    restricted_uses: Sequence[str],
    independence_treatment: Mapping[str, Any],
    restrictions: Sequence[str],
    invalidation_conditions: Sequence[Mapping[str, Any]],
    immutable_historical_references: Sequence[Mapping[str, Any]],
    untrusted_material_locators: Sequence[Mapping[str, Any]],
    historical_advisory_scope: Mapping[str, Any],
) -> ContextRevisionRecord:
    """Issue one v2 Context with advisory-history scope and immutable references."""

    authority = _require_direct_authority(
        authority,
        action="first-class Context",
    )

    def ordered_mapping_sequence(
        values: Sequence[Mapping[str, Any]],
        label: str,
    ) -> list[Mapping[str, Any]]:
        items = _require_mapping_sequence(values, label)
        return [deep_thaw(item) for item in sorted(items, key=canonical_json_bytes)]

    def ordered_text_sequence(values: Sequence[str], label: str) -> list[str]:
        if isinstance(values, (str, bytes, bytearray)):
            raise ValueError(f"{label} must be an array")
        result = tuple(_require_text(item, label) for item in values)
        if len(result) != len(set(result)):
            raise ValueError(f"{label} must not contain duplicates")
        return sorted(result)

    scope = _require_object(historical_advisory_scope, "historical_advisory_scope")
    if set(scope) != _CONTEXT_HISTORICAL_SCOPE_KEYS:
        raise ValueError("historical_advisory_scope has the wrong closed shape")
    normalized_scope = {
        "assignment_mode": scope["assignment_mode"],
        "search_lens": scope["search_lens"],
        "source_families": ordered_text_sequence(
            scope["source_families"],
            "historical_advisory_scope.source_families",
        ),
        "known_omissions": ordered_text_sequence(
            scope["known_omissions"],
            "historical_advisory_scope.known_omissions",
        ),
        "coverage_limits": ordered_text_sequence(
            scope["coverage_limits"],
            "historical_advisory_scope.coverage_limits",
        ),
    }
    document = {
        "schema_version": CONTEXT_REVISION_SCHEMA_VERSION_V2,
        "kind": "first_class_context",
        "project_id": authority.project_id,
        "mission_id": authority.mission_id,
        "context_id": _require_text(context_id, "context_id"),
        "purpose": _require_text(purpose, "purpose"),
        "question": _require_text(question, "question"),
        "indispensable_ground": ordered_mapping_sequence(
            indispensable_ground,
            "indispensable_ground",
        ),
        "owner_source_references": ordered_mapping_sequence(
            owner_source_references,
            "owner_source_references",
        ),
        "known_omissions": ordered_text_sequence(
            known_omissions,
            "known_omissions",
        ),
        "restricted_uses": ordered_text_sequence(
            restricted_uses,
            "restricted_uses",
        ),
        "independence_treatment": deep_thaw(
            _require_object(independence_treatment, "independence_treatment")
        ),
        "restrictions": ordered_text_sequence(restrictions, "restrictions"),
        "invalidation_conditions": ordered_mapping_sequence(
            invalidation_conditions,
            "invalidation_conditions",
        ),
        "immutable_historical_references": ordered_mapping_sequence(
            immutable_historical_references,
            "immutable_historical_references",
        ),
        "untrusted_material_locators": ordered_mapping_sequence(
            untrusted_material_locators,
            "untrusted_material_locators",
        ),
        "historical_advisory_scope": normalized_scope,
    }
    return _record_from_document(document)


def _normalized_scientific_treatments(
    treatments: Mapping[str, Any],
    *,
    this_evidence_reference: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    if not isinstance(treatments, Mapping):
        raise ValueError("scientific treatments must be an object")
    normalized = deep_thaw(treatments)
    for treatment_id, treatment in normalized.items():
        _require_text(treatment_id, "treatment_id")
        if not isinstance(treatment, Mapping):
            raise ValueError("scientific treatment must be an object")
        for source in _require_mapping_sequence(treatment.get("sources"), "sources"):
            if source.get("reference") == {"source": "this_evidence"}:
                if this_evidence_reference is None:
                    raise ValueError("this_evidence is only available in a paired publication")
                if context_reference_key(this_evidence_reference)[0] != "evidence":
                    raise ValueError("this_evidence must resolve to exact ordinary Evidence")
                source["reference"] = deep_thaw(this_evidence_reference)
            roles = source.get("roles")
            if not isinstance(roles, (list, tuple)) or any(not isinstance(role, str) for role in roles):
                raise ValueError("scientific source roles must be an array of strings")
            # Canonicalize a set without silently discarding duplicate input.
            if len(roles) != len(set(roles)):
                raise ValueError("scientific source roles must not repeat")
            source["roles"] = sorted(roles)
            selection = source.get("selection")
            if isinstance(selection, Mapping) and selection.get("mode") == "treatments":
                values = selection.get("treatment_ids")
                if not isinstance(values, (list, tuple)):
                    raise ValueError("treatment_ids must be an array")
                values = [_require_text(value, "treatment_ids") for value in values]
                if len(values) != len(set(values)):
                    raise ValueError("treatment_ids must not repeat")
                selection["treatment_ids"] = sorted(values)
        _validate_scientific_treatment(treatment, f"treatments.{treatment_id}")
    return normalized


def issue_context_revision_v3(
    *,
    authority: DirectExecutiveEpochAuthority,
    context_id: str,
    purpose: str,
    question: str,
    treatments: Mapping[str, Any],
    exposed_treatments: Sequence[Mapping[str, Any]],
    known_omissions: Sequence[str],
    restricted_uses: Sequence[str],
    independence_treatment: Mapping[str, Any],
    restrictions: Sequence[str],
) -> ContextRevisionRecord:
    """Issue scientific meaning, not a historical grant or proof verdict."""

    authority = _require_direct_authority(authority, action="scientific Context")

    def texts(values: Sequence[str], label: str) -> list[str]:
        if not isinstance(values, (list, tuple)):
            raise ValueError(f"{label} must be an array")
        normalized = [_require_text(value, label) for value in values]
        if len(normalized) != len(set(normalized)):
            raise ValueError(f"{label} must not contain duplicates")
        return sorted(normalized)

    document = {
        "schema_version": CONTEXT_REVISION_SCHEMA_VERSION_V3,
        "kind": "first_class_context",
        "project_id": authority.project_id,
        "mission_id": authority.mission_id,
        "context_id": _require_text(context_id, "context_id"),
        "purpose": _require_text(purpose, "purpose"),
        "question": _require_text(question, "question"),
        "treatments": _normalized_scientific_treatments(treatments),
        "exposed_treatments": sorted(
            (deep_thaw(value) for value in _require_mapping_sequence(exposed_treatments, "exposed_treatments")),
            key=canonical_json_bytes,
        ),
        "known_omissions": texts(known_omissions, "known_omissions"),
        "restricted_uses": texts(restricted_uses, "restricted_uses"),
        "restrictions": texts(restrictions, "restrictions"),
        "independence_treatment": deep_thaw(independence_treatment),
    }
    return _record_from_document(document)


@dataclass(frozen=True, slots=True)
class PreparedScientificContextUpdate:
    """Full immutable affected payload and exact preimage for the sole writer.

    This is preparation, not publication or current-head authentication. The
    Store must check preimages, source access, dependencies and exposure in its
    existing transaction, after exact-replay lookup.
    """

    record: ContextRevisionRecord
    expected_head: Mapping[str, Any] | None
    changed_treatment_ids: tuple[str, ...]
    dependency_heads: Mapping[str, tuple[int, str]]
    changed: bool

    def __post_init__(self) -> None:
        if type(self.record) is not ContextRevisionRecord:
            raise TypeError("scientific update requires exact ContextRevisionRecord")
        self.record.verify_issued()
        if self.record.document["schema_version"] != CONTEXT_REVISION_SCHEMA_VERSION_V3:
            raise ValueError("scientific update requires Context v3")
        if self.expected_head is not None:
            kind, identity, _, _ = context_reference_key(self.expected_head)
            if kind != "context" or identity != self.record.document["context_id"]:
                raise ValueError("scientific expected head has the wrong Context identity")
        object.__setattr__(self, "expected_head", deep_freeze(self.expected_head))
        object.__setattr__(
            self, "changed_treatment_ids",
            _require_text_sequence(self.changed_treatment_ids, "changed_treatment_ids"),
        )
        if any(key not in self.record.document["treatments"] for key in self.changed_treatment_ids):
            raise ValueError("changed treatment guard names an absent treatment")
        expected = _scientific_context_dependency_heads(self.record.document, self.changed_treatment_ids)
        if self.dependency_heads != expected:
            raise ValueError("scientific update dependency heads are stale")
        object.__setattr__(self, "dependency_heads", deep_freeze(self.dependency_heads))
        if not isinstance(self.changed, bool):
            raise TypeError("scientific update changed state must be boolean")


def _scientific_context_dependency_heads(
    document: Mapping[str, Any], treatment_ids: Sequence[str],
) -> Mapping[str, tuple[int, str]]:
    guards: dict[str, tuple[int, str]] = {}
    for edge in context_reference_projection(
        document, purpose="concurrency", treatment_ids=treatment_ids,
    ):
        kind, identity, revision, digest = context_reference_key(edge["reference"])
        if kind not in {"mission", "branch", "strategy", "context", "candidate", "evidence"}:
            raise ValueError("current-head dependency must name a mutable Store owner")
        key = f"{kind}:{identity}"
        value = (revision, digest)
        if key in guards and guards[key] != value:
            raise ValueError("changed treatments disagree about an observed current head")
        guards[key] = value
    return deep_freeze(dict(sorted(guards.items())))


def prepare_scientific_context_update(
    *,
    authority: DirectExecutiveEpochAuthority,
    update: Mapping[str, Any],
    previous: PersistedContextRevision | None,
    this_evidence_reference: Mapping[str, Any] | None = None,
) -> PreparedScientificContextUpdate:
    """Prepare scientific meaning under the current direct Executive Epoch."""

    authority = _require_direct_authority(authority, action="scientific Context update")
    return prepare_scientific_context_update_for_scope(
        project_id=authority.project_id, mission_id=authority.mission_id,
        update=update, previous=previous,
        this_evidence_reference=this_evidence_reference,
    )


def prepare_scientific_context_update_for_scope(
    *, project_id: str, mission_id: str, update: Mapping[str, Any],
    previous: PersistedContextRevision | None,
    this_evidence_reference: Mapping[str, Any] | None = None,
) -> PreparedScientificContextUpdate:
    """Pure semantic preparation; this confers no publication authority.

    Unmentioned semantic values survive exactly. Complete affected payloads
    are copied, validated and hashed, so this is O(affected Context size), not
    delta-local storage. The live-Epoch and stopped-owner commands separately
    authenticate their own authority, source access and exact preimages inside
    the Store transaction. Never fabricate an Epoch for stopped maintenance.
    """

    project_id = _require_text(project_id, "project_id")
    mission_id = _require_text(mission_id, "mission_id")
    if not isinstance(update, Mapping) or set(update) != {"context_id", "expected_head", "create", "patch"}:
        raise ValueError("scientific Context update has the wrong closed shape")
    selector = _require_text(update["context_id"], "context_id")
    if not selector.startswith("context:"):
        raise ValueError("scientific Context update requires a context: selector")
    context_id = _require_text(selector[len("context:"):], "context_id")
    if context_id.startswith("context:"):
        raise ValueError("scientific Context update has a repeated prefix")
    expected_head = update["expected_head"]
    creating = update["create"] is not None
    if creating == (update["patch"] is not None):
        raise ValueError("scientific update requires exactly one of create or patch")
    if creating:
        if expected_head is not None or previous is not None:
            raise ValueError("scientific Context creation requires absent identity")
        if not isinstance(update["create"], Mapping):
            raise ValueError("scientific Context creation requires a complete document")
        document = deep_thaw(update["create"])
        document["treatments"] = _normalized_scientific_treatments(
            document.get("treatments"), this_evidence_reference=this_evidence_reference,
        )
        record = _record_from_document(document)
        changed_ids = tuple(sorted(document["treatments"]))
        changed = True
    else:
        if type(previous) is not PersistedContextRevision or expected_head is None:
            raise ValueError("scientific patch requires the exact persisted preimage")
        reference_key = context_reference_key(expected_head)
        actual = (
            "context", str(previous.record.document["context_id"]), previous.revision,
            previous.payload_digest,
        )
        if reference_key != actual or reference_key[1] != context_id:
            raise ValueError("scientific Context expected head differs from frozen preimage")
        if previous.record.document["schema_version"] != CONTEXT_REVISION_SCHEMA_VERSION_V3:
            raise ValueError("scientific patch cannot implicitly convert legacy Context")
        patch = update["patch"]
        if not isinstance(patch, Mapping) or set(patch) != _SCIENTIFIC_PATCH_KEYS:
            raise ValueError("scientific Context patch has the wrong closed shape")
        insert = _normalized_scientific_treatments(patch["insert"], this_evidence_reference=this_evidence_reference)
        replacements = _normalized_scientific_treatments(patch["replace"], this_evidence_reference=this_evidence_reference)
        removals: set[str] = set()
        for item in _require_mapping_sequence(patch["remove"], "remove"):
            if set(item) != {"treatment_id", "reason"}:
                raise ValueError("treatment removal has the wrong closed shape")
            treatment_id = _require_text(item["treatment_id"], "treatment_id")
            _require_text(item["reason"], "treatment removal reason")
            if treatment_id in removals:
                raise ValueError("treatment removal repeats a key")
            removals.add(treatment_id)
        if set(insert) & set(replacements) or set(insert) & removals or set(replacements) & removals:
            raise ValueError("scientific patch has conflicting treatment edits")
        document = deep_thaw(previous.record.document)
        treatments = document["treatments"]
        if any(key in treatments for key in insert):
            raise ValueError("treatment insertion requires absence")
        if any(key not in treatments for key in set(replacements) | removals):
            raise ValueError("treatment replacement/removal requires presence")
        changed_ids = tuple(sorted(
            set(insert) | {key for key, value in replacements.items() if value != treatments[key]}
        ))
        for key in removals:
            del treatments[key]
        treatments.update(insert)
        treatments.update(replacements)
        memberships = {_exposure_key(item): item for item in document["exposed_treatments"]}
        touched: set[tuple[str, str]] = set()
        for field, removal in (("exposure_add", False), ("exposure_remove", True)):
            for item in _require_mapping_sequence(patch[field], field):
                key = _exposure_key(item, removal=removal)
                if key in touched:
                    raise ValueError("scientific patch has conflicting exposure edits")
                touched.add(key)
                if removal:
                    if key not in memberships:
                        raise ValueError("exposure removal requires presence")
                    del memberships[key]
                else:
                    if key in memberships:
                        raise ValueError("exposure addition requires absence")
                    memberships[key] = deep_thaw(item)
        # Preserve order/values of unmentioned authored memberships; inserting
        # or removing a different pair must not rewrite their semantic value.
        document["exposed_treatments"] = list(memberships.values())
        metadata = patch["metadata_replace"]
        if not isinstance(metadata, Mapping) or not set(metadata) <= _SCIENTIFIC_METADATA_KEYS:
            raise ValueError("scientific metadata replacement has the wrong closed shape")
        document.update(deep_thaw(metadata))
        record = _record_from_document(document)
        changed = record.digest_sha256 != previous.record.digest_sha256
    if record.document["schema_version"] != CONTEXT_REVISION_SCHEMA_VERSION_V3:
        raise ValueError("scientific creation requires Context v3")
    if (
        record.document["context_id"] != context_id
        or record.document["project_id"] != project_id
        or record.document["mission_id"] != mission_id
    ):
        raise ValueError("scientific Context update belongs to another identity or Mission")
    return PreparedScientificContextUpdate(
        record=record,
        expected_head=expected_head,
        changed_treatment_ids=changed_ids,
        dependency_heads=_scientific_context_dependency_heads(record.document, changed_ids),
        changed=changed,
    )


def _context_store_dependency_heads(
    record: ContextRevisionRecord,
) -> Mapping[str, tuple[int, str]]:
    """Project only mutable Store-owner references into concurrency guards."""

    owner_prefixes = {
        "mission": "mission",
        "branch": "branch",
        "strategy": "strategy",
        "context": "context",
        "candidate": "candidate",
        "evidence": "evidence",
    }
    dependencies: dict[str, tuple[int, str]] = {}
    for reference in record.dependency_heads:
        kind, identity, revision, digest = context_reference_key(reference)
        prefix = owner_prefixes.get(kind)
        if prefix is not None:
            dependencies[f"{prefix}:{identity}"] = (revision, digest)
    return deep_freeze(dict(sorted(dependencies.items())))


def commit_context_revision(
    store: Any,
    *,
    authority: DirectExecutiveEpochAuthority,
    record: ContextRevisionRecord,
    lease: Any,
    actor: str,
    expected_head_revision: int | None = None,
    expected_head_payload_digest: str | None = None,
    command_id: str | None = None,
) -> Any:
    """Commit one first-class Context through its target-local Store owner."""

    if type(record) is not ContextRevisionRecord:
        raise TypeError("commit_context_revision requires ContextRevisionRecord")
    record.verify_issued()
    if record.document["schema_version"] == CONTEXT_REVISION_SCHEMA_VERSION_V3:
        raise ValueError("scientific Context requires the scientific publication owner")
    authority = _require_direct_authority(
        authority,
        action="Context commit",
    )
    if (
        record.document["project_id"] != authority.project_id
        or record.document["mission_id"] != authority.mission_id
    ):
        raise ValueError("Context revision belongs to another current Mission")
    if (expected_head_revision is None) != (expected_head_payload_digest is None):
        raise ValueError("expected Context head revision and digest travel together")
    if expected_head_revision is not None:
        _require_positive_int(
            expected_head_revision,
            "expected Context head revision",
        )
        _require_sha256(
            expected_head_payload_digest,
            "expected Context head payload digest",
        )
    dependency_heads = {
        key: (int(value[0]), str(value[1]))
        for key, value in _context_store_dependency_heads(record).items()
    }
    return store.commit_context_revision(
        executive_epoch_id=authority.executive_epoch_id,
        context_id=str(record.document["context_id"]),
        payload=record.document,
        expected_head_revision=expected_head_revision,
        expected_head_payload_digest=expected_head_payload_digest,
        dependency_heads=dependency_heads,
        lease=lease,
        command_id=(
            command_id
            or f"context:{record.document['context_id']}:{record.digest_sha256}"
        ),
        actor=_require_text(actor, "Context actor"),
        expected_canonical_authority_digest=authority.canonical_authority_digest,
    )


def read_context_revision(
    store: Any,
    *,
    context_id: str,
    revision: int | None = None,
) -> PersistedContextRevision:
    """Read one current or historical first-class Context owner revision."""

    context_id = _require_text(context_id, "context_id")
    if revision is not None:
        _require_positive_int(revision, "Context revision")
    stored = store.read_context_revision(context_id, revision=revision)
    record = _record_from_document(stored["payload"])
    if record.document["context_id"] != context_id:
        raise ValueError("stored Context payload identity differs from its owner key")
    current = stored if revision is None else store.read_context_revision(context_id)
    return PersistedContextRevision(
        record=record,
        revision=int(stored["revision"]),
        payload_digest=str(stored["payload_digest"]),
        predecessor_revision=(
            None
            if stored["predecessor_revision"] is None
            else int(stored["predecessor_revision"])
        ),
        created_actor=str(stored["created_actor"]),
        created_at=str(stored["created_at"]),
        is_current_head=(
            int(current["revision"]) == int(stored["revision"])
            and str(current["payload_digest"]) == str(stored["payload_digest"])
        ),
    )
