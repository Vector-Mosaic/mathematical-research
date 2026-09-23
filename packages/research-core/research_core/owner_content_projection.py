"""Pure, caller-specific text projections for the Store-owned content index.

Inputs are already authenticated owner values, not storage handles.  This module
does not read a Store, choose a head/cut, expand references, fetch captured bytes,
or decide mathematical relevance.  Existing outward projectors remain the
authority for their strings; function-local imports reuse them without adding a
Store -> interface import cycle during module initialization.
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Mapping, Sequence
from typing import Any

from .json_support import canonical_json_bytes
from .research_model import deep_freeze, deep_thaw


SEARCH_FIELDS = ("id", "title", "content", "relationships")
ROOT_KINDS = (
    "mission", "strategy", "branch", "candidate", "context", "evidence",
    "session", "capture", "capture-annotation",
)
DELEGATED_FAMILY_KINDS = deep_freeze({
    "branches": "branch", "candidates": "candidate", "strategies": "strategy",
    "contexts": "context", "evidence": "evidence",
    "capture_annotations": "capture-annotation", "captures": "capture",
    "capture_artifacts": "capture-artifact",
})
ROOT_DEFAULT_FIELDS = ("content", "id", "title")
DELEGATED_LEGACY_DEFAULT_FIELDS = ("title", "content")
PROJECTION_FIELDS = deep_freeze({
    "root": {kind: list(SEARCH_FIELDS) for kind in ROOT_KINDS},
    "delegated": {
        kind: list(SEARCH_FIELDS) for kind in DELEGATED_FAMILY_KINDS.values()
    },
})
CHECKPOINT_PRESENCE_FIELDS = deep_freeze({
    "candidate": ["obligations", "gaps", "objections", "circularity_risks"],
    "context": ["known_omissions"],
})
PROJECTION_PROFILE = deep_freeze({
    "schema_version": "mathematical_research.owner_content_projection.v2",
    "access_namespaces": ["lexical", "current_field_presence", "owner_inventory", "capture_epoch_kind"],
    "index_codec": {
        "hash_domain": "mathematical_research.owner_content_index.v2",
        "digest": "sha256_canonical_json",
        "lexical_grams": [1, 2, 3],
        "positions": "unicode_codepoint_offsets_after_casefold_sorted_unique",
        "directory_order": "lexicographic_string_tuple",
        "posting_order": "kind_identity_numeric_revision_or_projection_origin; presence kind_identity",
        "lexical_scopes": ["retained_history", "current_at_cut"],
        "page_codec": "tuple-prefix-v1",
        "page_payload_bytes": 4096,
        "physical_kinds": ["range-cell", "range-branch", "range-source", "range-position-vector"],
        "page_commitment": ["domain", "profile", "kind", "scope", "count", "first", "last", "height", "used_bytes", "page_codec", "encoded_entries_or_children"],
        "source_factoring": "immutable_qualified_source_and_owner_local_field_map",
        "occurrence_manifest_roles": ["current", "events", "heads", "occurrences", "ordinary"],
        "retained_inverse": "qualified_occurrences_and_canonical_gram_presence_events",
        "large_values": "authenticated_ordered_chunk_tree",
        "large_positions": "oversized_delta_run_length_json_or_exact_positions",
        "descriptor": "v2_authenticated_manifest_summary_count_is_role_count",
    },
    "capture_epoch_kind": {
        "scope": "root_current_at_cut_only",
        "epoch": "exact_none_empty_or_epoch_nonempty_identity",
        "kinds": ["assignment", "output"],
        "value": "qualified_existing_capture_projection_reference",
    },
    "variants": deep_thaw(PROJECTION_FIELDS),
    "root_default_kinds": list(ROOT_KINDS),
    "root_default_fields": list(ROOT_DEFAULT_FIELDS),
    "root_default_page_size": 25,
    "delegated_source_families": deep_thaw(DELEGATED_FAMILY_KINDS),
    "delegated_default_families": "all_exact_granted_families",
    "delegated_fields": "required_for_history_search",
    "delegated_legacy_default_fields": list(DELEGATED_LEGACY_DEFAULT_FIELDS),
    "delegated_default_page_size": 50,
    "matching": "nonempty_unicode_casefold_substring_or_over_selected_fields",
    "root_owner_content": "semantic_owner_document",
    "root_owner_relationships": "exact_references_in_as_digestless_handles_context_v3_discovery_roles",
    "root_capture_content": "complete_authenticated_cut_dependent_descriptor",
    "root_capture_relationships": "native_lineage_json_including_null",
    "delegated_content": "direct_readable_item_metadata_only",
    "delegated_strategy": "readable_research_value_plus_formal_request_projection",
    "delegated_relationships": "historical_explicit_edges_declared_relationship_kinds",
    "root_scope": "current_at_first_page_cut",
    "delegated_scope": "retained_revisions_and_immutable_origins_at_grant_cut",
    "inventory_scopes": {
        "root": ["current_at_first_page_cut"],
        "delegated": ["retained_at_grant_cut", "current_heads_at_grant_cut"],
    },
    "bootstrap_membership": {
        "origin_commit": "immutable_owner_or_capture_creation_origin",
        "selected_at_commit": "exact_journal_authenticated_current_head_selection",
        "retired_at_commit": "exact_journal_authenticated_head_retirement_or_null",
        "current_at_prebootstrap_cut": "selected_at_commit_at_or_before_cut_and_no_later_selected_revision_or_retirement_at_cut",
        "interpretation": "derived_access_facts_not_scientific_status_or_new_authority",
    },
    "raw_body_search": False,
    "checkpoint_field_predicate": deep_thaw(CHECKPOINT_PRESENCE_FIELDS),
})
PROFILE_SHA256 = hashlib.sha256(canonical_json_bytes(PROJECTION_PROFILE)).hexdigest()
# Both integration call sites denote this one profile, not separate registries.
PROFILE_DIGEST = PROFILE_SHA256
_REFERENCE_KEYS = {"kind", "identity", "revision", "payload_sha256"}
_ARTIFACT_KEYS = {"ordinal", "role", "logical_name", "blob_sha256"}
_CAPTURE_KEYS = {
    "capture_id", "project_id", "mission_id", "executive_epoch_id",
    "capture_kind", "observation_id", "assignment_id", "provenance",
    "completion", "artifacts",
}
_ROOT_CAPTURE_KEYS = {
    "id", "kind", "title", "capture_kind", "origin_epoch_id",
    "origin_epoch_state", "late_classification", "cut_relation", "pending_state",
    "native_lineage", "artifacts", "failed_interval_or_late_output",
    "trust_class", "completeness",
}
_ROOT_ARTIFACT_KEYS = {"handle", "ordinal", "role", "logical_name", "pending"}
_WITHHELD_BODY = "withheld by delegated historical read policy"


def projection_profile_digest() -> str:
    """Return the canonical closed projection contract's SHA-256 identity."""
    return PROFILE_SHA256


def _json(value: Any) -> str:
    return canonical_json_bytes(value).decode("utf-8")


def _text(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value:
        raise ValueError(f"{label} must be nonempty text")
    return value


def _reference(reference: Mapping[str, Any], kinds: Sequence[str]) -> tuple[str, str, int]:
    if not isinstance(reference, Mapping) or set(reference) != _REFERENCE_KEYS:
        raise ValueError("search source must have one exact owner reference")
    kind = _text(reference["kind"], "reference kind")
    identity = _text(reference["identity"], "reference identity")
    revision = reference["revision"]
    if kind not in kinds:
        raise ValueError("owner kind is not supported by this search variant")
    if type(revision) is not int or revision < 1:
        raise ValueError("owner search revision must be a positive integer")
    digest = reference["payload_sha256"]
    if not isinstance(digest, str) or re.fullmatch(r"[0-9a-f]{64}", digest) is None:
        raise ValueError("owner search reference must carry its exact digest")
    return kind, identity, revision


def project_root_owner_fields(
    reference: Mapping[str, Any], document: Mapping[str, Any],
) -> dict[str, str]:
    """Match root `_search_fields`; this is discovery, not proof-basis closure."""
    from .executive_orientation import semantic_owner_document
    from .mission_interface import MissionInterface

    kind, identity, revision = _reference(reference, tuple(k for k in ROOT_KINDS if k != "capture"))
    if kind == "context" and document.get("schema_version") == 3:
        from .context_revision import context_reference_projection

        exact_references = tuple(
            item["reference"]
            for item in context_reference_projection(
                document, purpose="discovery", whole_context=True,
            )
        )
    else:
        exact_references = MissionInterface._exact_references_in(document)
    relationships = [
        {"id": f"{ref['kind']}:{ref['identity']}", "revision": ref["revision"]}
        for ref in exact_references
    ]
    return {
        "id": f"{kind}:{identity}@{revision}", "title": identity,
        "content": _json(semantic_owner_document(kind, document)),
        "relationships": _json(relationships),
    }


def project_root_capture_fields(descriptor: Mapping[str, Any]) -> dict[str, str]:
    """Index a complete same-cut descriptor; never infer pending/epoch state."""
    if not isinstance(descriptor, Mapping) or set(descriptor) != _ROOT_CAPTURE_KEYS:
        raise ValueError("root Capture search requires the closed derived descriptor")
    if descriptor["kind"] != "capture" or descriptor["completeness"] != "descriptor":
        raise ValueError("root Capture search does not accept captured content")
    if descriptor["trust_class"] != "custody_descriptor":
        raise ValueError("root Capture search requires custody metadata")
    for artifact in descriptor["artifacts"]:
        if not isinstance(artifact, Mapping) or set(artifact) != _ROOT_ARTIFACT_KEYS:
            raise ValueError("root Capture search accepts artifact metadata only")
    return {
        "id": _text(descriptor["id"], "Capture id"),
        "title": _text(descriptor["title"], "Capture title"),
        "content": _json(descriptor),
        "relationships": _json(descriptor["native_lineage"]),
    }


def _artifact_metadata(artifact: Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(artifact, Mapping) or set(artifact) != _ARTIFACT_KEYS:
        raise ValueError("Capture artifact search accepts only its closed descriptor")
    if type(artifact["ordinal"]) is not int or artifact["ordinal"] < 0:
        raise ValueError("Capture artifact ordinal must be nonnegative")
    _text(artifact["role"], "artifact role")
    _text(artifact["logical_name"], "artifact logical name")
    return deep_thaw(artifact)


def _capture_metadata(capture: Mapping[str, Any]) -> dict[str, Any]:
    from .mission_interface import MissionInterface

    if not isinstance(capture, Mapping) or set(capture) not in (
        _CAPTURE_KEYS, _CAPTURE_KEYS | {"native_lineage"},
    ):
        raise ValueError("Capture search requires the closed metadata payload")
    document = {key: deep_thaw(capture[key]) for key in _CAPTURE_KEYS}
    document["artifacts"] = [_artifact_metadata(item) for item in capture["artifacts"]]
    lineage = MissionInterface._native_capture_lineage(document)
    if "native_lineage" in capture and capture["native_lineage"] != lineage:
        raise ValueError("Capture native lineage must match its authenticated metadata")
    if lineage is not None:
        document["native_lineage"] = lineage
    return document


def project_delegated_fields(
    item: Mapping[str, Any], *, authoritative_document: Mapping[str, Any],
) -> dict[str, str]:
    """Match delegated search over an existing metadata-only readable item.

    The item is already the owner's outward projection, not a raw payload.  Raw
    Capture families are additionally checked against their descriptor-only
    projection so this helper cannot be used as an artifact-body indexing lane.
    """
    from .mission_interface import MissionInterface, _readable_content
    from .mission_operation_contract import HISTORICAL_RELATIONSHIP_KINDS

    kind = _text(item.get("kind"), "delegated item kind")
    if kind not in PROJECTION_FIELDS["delegated"]:
        raise ValueError("owner kind is outside delegated search families")
    content = _text(item.get("readable_content"), "delegated readable content")
    if kind == "capture":
        expected = _capture_metadata(authoritative_document)
        if content != _readable_content(expected):
            raise ValueError("delegated Capture search cannot index captured bodies")
    elif kind == "capture-artifact":
        if set(authoritative_document) != {"capture_id", "artifact"}:
            raise ValueError("delegated artifact authority must be metadata only")
        expected = {
            "capture_id": authoritative_document["capture_id"],
            "artifact": _artifact_metadata(authoritative_document["artifact"]),
            "raw_body": _WITHHELD_BODY,
        }
        if content != _readable_content(expected):
            raise ValueError("delegated artifact search cannot index captured bodies")
    return {
        "id": _text(item.get("id"), "delegated item id"),
        "title": _text(item.get("title"), "delegated item title"),
        "content": content,
        "relationships": _json(MissionInterface._historical_explicit_edges(
            item, HISTORICAL_RELATIONSHIP_KINDS,
            authoritative_document=authoritative_document,
        )),
    }


def project_delegated_owner_fields(
    reference: Mapping[str, Any], document: Mapping[str, Any], *,
    strategy_formal_requests: Sequence[Mapping[str, Any]] | None = None,
) -> dict[str, str]:
    """Derive typed/Evidence fields from authenticated values, without reads.

    Strategy formal selectors must come from the existing owner projector; an
    omitted argument must not silently erase a formal-request search match.
    """
    from .mission_interface import _readable_content, _readable_research_value

    kinds = tuple(k for k in DELEGATED_FAMILY_KINDS.values() if k not in {"capture", "capture-artifact"})
    kind, identity, revision = _reference(reference, kinds)
    if kind == "strategy":
        if strategy_formal_requests is None:
            raise ValueError("Strategy search requires its owner-derived formal selectors")
        projected = _readable_research_value(document)
        projected["formal_requests"] = deep_thaw(strategy_formal_requests)
        content = _json(projected)
    elif kind == "capture-annotation":
        content = _readable_content({key: deep_thaw(document[key]) for key in (
            "annotation_id", "annotation_kind", "capture_id", "exact_scope", "lifecycle",
        )})
    else:
        content = _readable_content(document)
    authoritative = document
    if kind == "capture-annotation":
        authoritative = {key: deep_thaw(document[key]) for key in (
            "annotation_id", "capture_id", "exact_scope",
        )}
    return project_delegated_fields({
        "id": f"{kind}:{identity}@{revision}", "kind": kind, "title": identity,
        "readable_content": content,
    }, authoritative_document=authoritative)


def project_delegated_capture_fields(capture: Mapping[str, Any]) -> dict[str, str]:
    from .mission_interface import _readable_content

    document = _capture_metadata(capture)
    identity = _text(document["capture_id"], "Capture identity")
    return project_delegated_fields({
        "id": f"capture:{identity}", "kind": "capture", "title": identity,
        "readable_content": _readable_content(document),
    }, authoritative_document=document)


def project_delegated_artifact_fields(
    capture_id: str, artifact: Mapping[str, Any],
) -> dict[str, str]:
    from .mission_interface import _readable_content

    capture_id = _text(capture_id, "Capture identity")
    descriptor = _artifact_metadata(artifact)
    document = {"capture_id": capture_id, "artifact": descriptor}
    return project_delegated_fields({
        "id": f"capture-artifact:{capture_id}#{descriptor['ordinal']}",
        "kind": "capture-artifact", "title": descriptor["logical_name"],
        "readable_content": _readable_content({**document, "raw_body": _WITHHELD_BODY}),
    }, authoritative_document=document)


def current_checkpoint_fields(kind: str, document: Mapping[str, Any]) -> tuple[str, ...]:
    """Existing authored-field truthiness only; not a scientific question registry."""
    return tuple(field for field in CHECKPOINT_PRESENCE_FIELDS.get(kind, ()) if document.get(field, ()))


def matching_fields(
    values: Mapping[str, str], query: str, fields: Sequence[str],
) -> tuple[str, ...]:
    """Exact existing predicate, including JSON-boundary and Unicode matches."""
    folded = _text(query, "search query").casefold()
    if not fields or any(field not in SEARCH_FIELDS for field in fields):
        raise ValueError("search fields must be a nonempty supported selection")
    return tuple(field for field in fields if folded in values[field].casefold())
