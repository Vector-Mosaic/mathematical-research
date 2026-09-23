"""Read-only root discovery over existing owner revisions and journal facts.

Collection cursors retain an immutable first-page cut, not a database session.
The live Executive authority is checked independently on every call. Proof
attention instead pins its exact live projection: its mutable binding is not
misrepresented as historical state. Neither mechanism creates research state.
"""
from __future__ import annotations

import base64
import hashlib
import json
import re
from collections.abc import Iterator, Mapping
from typing import Any

from .json_support import canonical_json_bytes
from .research_model import deep_thaw
from .workspace_schema import IdentityKind, TypedWorkspaceId
from .workspace_store import (
    RawCaptureArtifactUnavailableError,
    StaleCommandError,
    WorkspaceIntegrityError,
    _ROOT_HOOK_FIELD_CLASSES,
)


OWNER_KINDS = ("mission", "strategy", "branch", "candidate", "context", "evidence", "session")
_FAMILIES = {"branches": "branch", "candidates": "candidate", "strategies": "strategy", "contexts": "context", "evidence": "evidence", "capture_annotations": "capture-annotation"}
_ARRAY_FILTERS = {"kinds", "fields", "relations", "hook_classes", "owner_kinds", "epoch_ids", "thread_ids", "capture_kinds", "origin_epoch_states", "late_classifications"}


def _error(code: str, message: str) -> None:
    from .mission_interface import MissionInterfaceError
    raise MissionInterfaceError(code, message)


def _handle(kind: str, identity: str, revision: int | None = None) -> str:
    return f"{kind}:{identity}" + ("" if revision is None else f"@{revision}")


def _ref_handle(reference: Mapping[str, Any]) -> str:
    return _handle(str(reference["kind"]), str(reference["identity"]), int(reference["revision"]))


def _checkpoint(service: Any, checkpoint_id: str | None = None) -> Mapping[str, Any] | None:
    checkpoint = service._store.read_continuation_checkpoint(
        **({"latest_mission_id": service._mission_id} if checkpoint_id is None else {"checkpoint_id": checkpoint_id})
    )
    if checkpoint is not None and checkpoint["mission_id"] != service._mission_id:
        _error("mission_retrieval_wrong_mission", "The selected checkpoint belongs to another Mission.")
    if checkpoint_id is not None and checkpoint is None:
        _error("mission_retrieval_not_found", "The selected checkpoint is absent.")
    return checkpoint


def _retirements(facts: Mapping[str, Any]) -> dict[tuple[str, str], tuple[int, Mapping[str, Any]]]:
    return {
        (str(row["kind"]), str(row["object_id"])): (int(event["project_commit"]), row)
        for event in facts["post_cut_transitions"] for row in event["retired_heads"]
    }


def _head_selectors(facts: Mapping[str, Any], cut: int) -> list[tuple[str, str, int]]:
    """Reconstruct heads at a fixed cut; retained inert history is never a head."""
    selected: dict[tuple[str, str], int] = {}
    for row in facts["head_history"]:
        revisions = [value for value in row["revisions"] if int(value["project_commit"]) <= cut]
        if revisions:
            selected[(str(row["kind"]), str(row["identity"]))] = int(revisions[-1]["revision"])
    for key, (retired_at, row) in _retirements(facts).items():
        if retired_at <= cut:
            selected.pop(key, None)
        else:
            # The journal-authenticated retirement names the exact formerly
            # current revision. Select its history, not every retained record.
            history = [item for item in facts["retained_history"] if
                       _FAMILIES.get(str(item["source_family"])) == key[0]
                       and item["identity"] == key[1] and int(item["project_commit"]) <= cut]
            if history:
                selected[key] = max(int(item["revision"]) for item in history)
    for item in facts["retained_history"]:
        if item["source_family"] == "capture_annotations" and int(item["project_commit"]) <= cut:
            key = ("capture-annotation", str(item["identity"]))
            selected[key] = max(selected.get(key, 0), int(item["revision"]))
    return [(kind, identity, revision) for (kind, identity), revision in sorted(selected.items())]


def _owner(
    service: Any,
    kind: str,
    identity: str,
    revision: int | None,
    *,
    include_evidence_sources: bool = False,
) -> Mapping[str, Any] | None:
    if kind == "capture-annotation":
        row = service._store.read_capture_scope_annotation(identity, revision=revision)
        capture = service._store.read_raw_capture(str(row["capture_id"]))
        if capture["record"]["mission_id"] != service._mission_id:
            return None
        return {"document": deep_thaw(row), "reference": {"kind": kind, "identity": identity, "revision": row["revision"], "payload_sha256": row["payload_digest"]}}
    if kind not in OWNER_KINDS:
        return None
    if kind == "evidence":
        if revision is None:
            row = service._store.read_evidence_meaning_revision(identity)
            revision = int(row["record"]["revision"])
    elif revision is None:
        object_id = TypedWorkspaceId(IdentityKind(kind), identity)
        stored = service._store.get_head(object_id)
        if stored is None:
            return None
        revision = int(stored.reference.revision)
    assert revision is not None
    owner = service._read_recovery_owner_revision(
        kind=kind,
        identity=identity,
        revision=revision,
        **({"include_evidence_sources": True} if include_evidence_sources else {}),
    )
    return owner if owner["mission_id"] == service._mission_id else None


def _semantic_document(kind: str, document: Mapping[str, Any]) -> dict[str, Any]:
    from .executive_orientation import semantic_owner_document
    return semantic_owner_document(kind, document)


def evidence_source_navigation(sources: Any) -> dict[str, Any]:
    """Project already authenticated revision-owned source relations, not bytes."""
    return {
        "schema_version": "mathematical_research.evidence_source_navigation.v1",
        "relation": "recorded_capture_scopes",
        "items": [
            {"ordinal": int(source["source_ordinal"]),
             "handle": f"capture-artifact:{source['capture_id']}#{source['artifact_ordinal']}",
             "exact_scope": deep_thaw(source["exact_scope"])}
            for source in sources
        ],
    }


def _exact_read(
    service: Any, requested: str, *, source_project_commit: int | None = None,
) -> dict[str, Any]:
    kind, identity, revision = service._parse_retrieval_id(requested)
    try:
        if kind in OWNER_KINDS or kind == "capture-annotation":
            if kind == "strategy":
                from .mission_frontier import _stored_strategy, read_strategy_revision
                from .mission_interface import _formal_request_projection
                if revision is not None and source_project_commit is not None:
                    # The held schema12 cut authenticates the indexed origin,
                    # including retained root5 history. Do not rediscover that
                    # history through the generic Store revision proof or fall
                    # back to it when this index is absent or inconsistent.
                    indexed = service._store.read_indexed_observation_owner(
                        mission_id=service._mission_id, kind=kind,
                        identity=identity, revision=revision,
                        source_project_commit=source_project_commit,
                    )
                    if indexed is None:
                        raise StaleCommandError("Strategy is absent from this Mission")
                    selected = indexed["owner"]
                    reference = selected["reference"]
                    exact_strategy = _stored_strategy({
                        "strategy_id": reference["identity"],
                        "mission_id": selected["mission_id"],
                        "revision": reference["revision"],
                        "payload": selected["document"],
                        "payload_digest": reference["payload_sha256"],
                        **selected["revision_provenance"],
                    })
                else:
                    exact_strategy = read_strategy_revision(
                        service._store,
                        mission_id=service._mission_id,
                        strategy_id=identity,
                        revision=revision,
                    )
                owner = {
                    "reference": deep_thaw(exact_strategy.to_reference()),
                    "document": deep_thaw(exact_strategy.record.document),
                }
                document = _semantic_document(kind, owner["document"])
                document["formal_requests"] = _formal_request_projection(
                    exact_strategy
                )
            else:
                owner = _owner(
                    service,
                    kind,
                    identity,
                    revision,
                    include_evidence_sources=kind == "evidence",
                )
                document = (
                    None
                    if owner is None
                    else _semantic_document(kind, owner["document"])
                )
                if owner is not None and "evidence_sources" in owner:
                    assert document is not None
                    document["source_navigation"] = evidence_source_navigation(owner["evidence_sources"])
            item = None if owner is None else {
                "id": _ref_handle(owner["reference"]), "kind": kind, "title": identity,
                "readable_content": canonical_json_bytes(document).decode("utf-8"),
                "completeness": "complete_declared_semantic_projection", "trust_class": "semantic_owner_projection",
            }
        else:
            item = service._direct_readable_item(requested)
            if item is not None:
                item = {**deep_thaw(item), "completeness": "complete" if kind == "capture-artifact" else "structured_view", "trust_class": "untrusted_raw_material" if kind == "capture-artifact" else "custody_descriptor"}
        if item is None:
            return {"requested_id": requested, "status": "not_found", "reason": "not_found_in_mission", "correction": "Select an exact handle from this Mission's bounded discovery."}
        return {"requested_id": requested, "status": "readable", **item}
    except RawCaptureArtifactUnavailableError as exc:
        return {"requested_id": requested, "status": "unavailable", "reason": str(exc.disposition), "correction": "This exact artifact is unavailable; keep its dependent decision unresolved and select other available material deliberately."}
    except (StaleCommandError, LookupError):
        return {"requested_id": requested, "status": "not_found", "reason": "not_found_in_mission", "correction": "Select an exact handle from this Mission's bounded discovery."}


def change_descriptors_in_snapshot(service: Any, facts: Mapping[str, Any] | None = None, *, cut: int | None = None, baseline: int | None = None) -> list[dict[str, Any]]:
    """Identity event counts/descriptors from validated history, including retirement."""
    facts = facts if facts is not None else service._store.read_direct_recovery_facts(cut_project_commit=None)
    cut = int(facts["observed_project_commit"]) if cut is None else cut
    if baseline is None:
        checkpoint = _checkpoint(service)
        if checkpoint is None:
            return []
        baseline = int(checkpoint["project_commit_no"])
    before = {(kind, identity): rev for kind, identity, rev in _head_selectors(facts, baseline)}
    after = {(kind, identity): rev for kind, identity, rev in _head_selectors(facts, cut)}
    events: dict[tuple[str, str], set[str]] = {}
    for event in facts["post_cut_transitions"]:
        if not baseline < int(event["project_commit"]) <= cut:
            continue
        for row in event["head_changes"]:
            key = (str(row["kind"]), str(row["identity"]))
            events.setdefault(key, set()).add("new" if int(row["revision"]) == 1 else "advanced")
        for row in event["evidence_head_advances"]:
            key = ("evidence", str(row["identity"]))
            events.setdefault(key, set()).add("new" if int(row["revision"]) == 1 else "advanced")
        for row in event["retired_heads"]:
            events.setdefault((str(row["kind"]), str(row["object_id"])), set()).add("retired")
    for row in facts["retained_history"]:
        if (
            row["source_family"] == "capture_annotations"
            and baseline < int(row["project_commit"]) <= cut
        ):
            events.setdefault(
                ("capture-annotation", str(row["identity"])),
                set(),
            ).add("new" if int(row["revision"]) == 1 else "advanced")
    capture_origins = {
        str(row["capture_id"]): row
        for row in facts.get("raw_capture_origins", ())
        if row["mission_id"] == service._mission_id
        and int(row["project_commit"]) <= cut
    }
    for capture_id, row in capture_origins.items():
        if baseline < int(row["project_commit"]):
            events.setdefault(("capture", capture_id), set()).add("new")
    result = []
    for (kind, identity), changes in sorted(events.items()):
        if kind not in {*OWNER_KINDS, "capture", "capture-annotation"}:
            continue
        if kind == "capture":
            if identity not in capture_origins:
                continue
            handle = _handle("capture", identity)
            result.append(
                {
                    "kind": kind,
                    "id": handle,
                    "before_handle": None,
                    "current_handle": handle,
                    "changes": [
                        value
                        for value in ("new", "advanced", "retired")
                        if value in changes
                    ],
                }
            )
            continue
        revision = after.get((kind, identity), before.get((kind, identity)))
        if revision is None:
            retired = _retirements(facts).get((kind, identity))
            revision = None if retired is None else int(retired[1]["revision"])
        owner = None if revision is None else _owner(service, kind, identity, revision)
        if owner is None or owner.get("recovery_internal") is not None:
            continue
        result.append({"kind": kind, "id": _handle(kind, identity),
                       "before_handle": None if (kind, identity) not in before else _handle(kind, identity, before[(kind, identity)]),
                       "current_handle": None if (kind, identity) not in after else _handle(kind, identity, after[(kind, identity)]),
                       "changes": [value for value in ("new", "advanced", "retired") if value in changes]})
    return result


def _owners(service: Any, facts: Mapping[str, Any], cut: int, kinds: set[str]) -> Iterator[Mapping[str, Any]]:
    for kind, identity, revision in _head_selectors(facts, cut):
        if kind in kinds:
            owner = _owner(service, kind, identity, revision)
            if owner is not None and owner.get("recovery_internal") is None:
                yield owner


def _coverage(service: Any, facts: Mapping[str, Any], cut: int) -> set[tuple[str, int]]:
    covered: set[tuple[str, int]] = set()
    for owner in _owners(service, facts, cut, {"evidence", "capture-annotation"}):
        document = owner["document"]
        if owner["reference"]["kind"] == "evidence":
            for source in document.get("sources", ()):
                if service._declares_complete_capture_artifact(source["exact_scope"]):
                    covered.add((str(source["capture_id"]), int(source["artifact_ordinal"])))
        else:
            scope = document["exact_scope"]
            if document["lifecycle"] == "active" and service._declares_complete_capture_artifact(scope) and isinstance(scope.get("artifact_ordinal"), int):
                covered.add((str(document["capture_id"]), int(scope["artifact_ordinal"])))
    return covered


def _captures(service: Any, facts: Mapping[str, Any], cut: int, checkpoint: Mapping[str, Any] | None) -> Iterator[dict[str, Any]]:
    from .executive_orientation import failed_output_is_relevant
    baseline = 0 if checkpoint is None else int(checkpoint["project_commit_no"])
    covered = _coverage(service, facts, cut)
    terminals = {str(row["executive_epoch_id"]): row for row in facts["terminal_epochs"] if row["mission_id"] == service._mission_id and int(row["project_commit"]) <= cut}
    for origin in sorted(facts["raw_capture_origins"], key=lambda row: str(row["capture_id"])):
        if origin["mission_id"] != service._mission_id or int(origin["project_commit"]) > cut:
            continue
        capture_id = str(origin["capture_id"])
        capture = service._store.read_raw_capture(capture_id)
        record = capture["record"]
        epoch = origin["executive_epoch_id"]
        terminal = terminals.get(str(epoch))
        terminal_kind = None if terminal is None else str(terminal["terminal_kind"])
        terminal_commit = None if terminal is None else int(terminal["project_commit"])
        late = ("unbound_to_epoch" if epoch is None else "epoch_not_terminal" if terminal is None else
                "at_or_before_terminal" if int(origin["project_commit"]) <= terminal_commit else
                "after_failed_terminal" if terminal_kind == "failed_before_checkpoint" else "after_checkpoint_terminal")
        artifacts = [{"handle": f"capture-artifact:{capture_id}#{item['ordinal']}", "ordinal": int(item["ordinal"]),
                      "role": str(item["role"]), "logical_name": str(item["logical_name"]),
                      "pending": (capture_id, int(item["ordinal"])) not in covered} for item in capture["artifacts"]]
        pending = any(item["pending"] for item in artifacts)
        lineage = service._native_capture_lineage(record)
        root_thread = record.get("provenance", {}).get("root_thread_id")
        if isinstance(root_thread, str) and root_thread:
            lineage = {**(lineage or {}), "root_thread_id": root_thread}
        yield {"id": _handle("capture", capture_id), "kind": "capture", "title": str(record["assignment_id"]),
               "capture_kind": str(origin["capture_kind"]), "origin_epoch_id": epoch,
               "origin_epoch_state": terminal_kind or ("unbound_to_epoch" if epoch is None else "epoch_not_terminal"),
               "late_classification": late, "cut_relation": "no_checkpoint" if checkpoint is None else "at_or_before_latest_checkpoint" if int(origin["project_commit"]) <= baseline else "after_latest_checkpoint",
               "pending_state": "current_pending" if pending else "fully_covered", "native_lineage": lineage,
               "artifacts": artifacts, "failed_interval_or_late_output": failed_output_is_relevant(
                   capture_kind=str(origin["capture_kind"]), origin_commit=int(origin["project_commit"]),
                   terminal_kind=terminal_kind, terminal_commit=terminal_commit, baseline_commit=baseline, pending=pending),
               "trust_class": "custody_descriptor", "completeness": "descriptor"}


def _normalized(selection: Mapping[str, Any]) -> dict[str, Any]:
    result = {key: deep_thaw(value) for key, value in selection.items() if key != "cursor"}
    result.setdefault("page_size", 25)
    for key in _ARRAY_FILTERS & result.keys():
        result[key] = sorted(result[key])
    mode = result["mode"]
    if mode in {"search", "inventory", "changes_since_checkpoint"}:
        result.setdefault("kinds", list(OWNER_KINDS) + (["capture", "capture-annotation"] if mode == "search" else []))
        result["kinds"] = sorted(result["kinds"])
    if mode == "search":
        result.setdefault("fields", ["content", "id", "title"])
    if mode == "changes_since_checkpoint":
        result.setdefault("relations", ["advanced", "new", "retired"])
    if mode == "hooks":
        result.setdefault("hook_classes", ["recombination", "reconsideration", "revival", "reversal", "unresolved"])
        result.setdefault("owner_kinds", sorted(OWNER_KINDS))
        result.setdefault("strategy_relation", "any")
    if mode == "captures":
        for key, default in (("view", "all"), ("cut_relation", "any"), ("pending_state", "any")):
            result.setdefault(key, default)
    return result


def _search_fields(service: Any, owner: Mapping[str, Any], fields: list[str]) -> dict[str, str]:
    from .owner_content_projection import project_root_owner_fields

    values = project_root_owner_fields(owner["reference"], owner["document"])
    return {field: values[field] for field in fields}


def _indexed_position(value: int | str | None) -> Any:
    """Unwrap an index seek only after the existing caller/cut MAC is checked."""
    if value is None or value == 0:
        return None
    try:
        parsed = json.loads(value) if isinstance(value, str) else None
    except (TypeError, ValueError):
        parsed = None
    if not isinstance(parsed, Mapping) or set(parsed) != {"owner_content_after", "index_binding"}:
        _error("mission_retrieval_cursor_invalid", "Indexed search continuation is invalid.")
    return parsed["owner_content_after"]


def _index_binding(batch: Mapping[str, Any]) -> dict[str, Any]:
    return {"descriptor_authentication_commit": batch["descriptor_authentication_commit"],
            "owner_content_index": deep_thaw(batch["owner_content_index"])}


def _validate_indexed_binding(position: int | str | None, batch: Mapping[str, Any]) -> None:
    if position is not None and position != 0:
        # The outer root/delegated MAC already bound this exact position to its
        # caller and source cut. Bootstrap search additionally binds the later
        # descriptor-authentication cut; it never pretends that index existed
        # at the historical source cut.
        _indexed_position(position)
        if json.loads(position)["index_binding"] != _index_binding(batch):
            _error("mission_retrieval_cursor_invalid", "Indexed search descriptor binding changed.")


def _seal_indexed_position(batch: Mapping[str, Any]) -> str:
    return canonical_json_bytes({"owner_content_after": deep_thaw(batch["next_after"]),
                                 "index_binding": _index_binding(batch)}).decode("utf-8")


def _indexed_search_page(
    service: Any, selection: Mapping[str, Any], *, cut: int,
    position: int | str | None,
) -> tuple[list[dict[str, Any]], str | None, bool]:
    """Read only authenticated lexical candidates, then check the real strings.

    Missing access is an explicit failure. This function deliberately has no
    owner/capture scan fallback and does not change the default nine families.
    """
    from .owner_content_projection import project_root_capture_fields

    batch = service._store.read_owner_content_search_page(
        mission_id=service._mission_id,
        projection_variant="root",
        kinds=tuple(selection["kinds"]), fields=tuple(selection["fields"]),
        query=str(selection["query"]), source_project_commit=cut,
        revision_scope="current_at_cut", after=_indexed_position(position),
        page_size=int(selection["page_size"]),
    )
    _validate_indexed_binding(position, batch)
    result: list[dict[str, Any]] = []
    folded = str(selection["query"]).casefold()
    for candidate in batch["items"]:
        reference = candidate["reference"]
        kind, identity = str(reference["kind"]), str(reference["identity"])
        if kind == "capture":
            # The Store authenticates the complete cut-dependent metadata
            # projection; captured artifact bodies are never searched here.
            item = deep_thaw(candidate["capture_descriptor"])
            values = project_root_capture_fields(item)
        else:
            owner = _owner(service, kind, identity, int(reference["revision"]))
            if owner is None or owner.get("recovery_internal") is not None:
                raise WorkspaceIntegrityError("indexed root source is outside its Mission")
            if owner["reference"] != reference:
                raise WorkspaceIntegrityError("indexed root source reference changed")
            values = _search_fields(service, owner, list(selection["fields"]))
            item = {"id": _ref_handle(reference), "kind": kind, "title": identity,
                    "completeness": "preview", "trust_class": "semantic_owner_descriptor"}
        matched = [field for field in selection["fields"]
                   if folded in values[field].casefold()]
        if not matched or sorted(matched) != sorted(candidate["matched_fields"]):
            raise WorkspaceIntegrityError("owner content index differs from authenticated search projection")
        item["match_provenance"] = [
            {"field": field, "query": selection["query"]} for field in matched
        ]
        if kind != "capture":
            value = values[matched[0]]
            offset = value.casefold().find(folded)
            item["preview"] = value[max(0, offset - 100):offset + len(folded) + 200]
        result.append(item)
    more = bool(batch["has_more"])
    after = batch["next_after"]
    if more and after is None:
        raise WorkspaceIntegrityError("indexed root search lost its exact continuation")
    sealed = _seal_indexed_position(batch) if more else None
    return result, sealed, more


def _discovery(service: Any, selection: Mapping[str, Any], facts: Mapping[str, Any], cut: int, checkpoint: Mapping[str, Any] | None) -> Iterator[dict[str, Any]]:
    kinds = set(selection["kinds"])
    for owner in _owners(service, facts, cut, kinds):
        ref = owner["reference"]
        item = {"id": _ref_handle(ref), "kind": str(ref["kind"]), "title": str(ref["identity"]), "completeness": "descriptor", "trust_class": "semantic_owner_descriptor"}
        yield item
    if "capture" in kinds:
        for item in _captures(service, facts, cut, checkpoint):
            yield item


def _checkpoint_items(service: Any, checkpoint: Mapping[str, Any] | None, section: str) -> Iterator[dict[str, Any]]:
    if checkpoint is None:
        return
    document = checkpoint["document"]
    if document["schema_version"] == 1:
        sections = {
            field: document[field]
            for field in (
                "transitive_owner_refs",
                "causal_pointers",
                "unresolved_pointers",
                "pending_capture_locators",
            )
        }
    elif section == "unresolved_pointers":
        # Compact v2 carries this operative set directly.  The other sections
        # are explicit historical reconstructions and are materialized only
        # when the caller actually selects one of them (or their summary).
        sections = {"unresolved_pointers": document["unresolved_pointers"]}
    else:
        sections = service._store.materialize_continuation_checkpoint_sections(
            checkpoint,
            recovery_facts=service._store.read_direct_recovery_facts(
                cut_project_commit=int(checkpoint["project_commit_no"])
            ),
        )
    if section == "summary":
        yield {"checkpoint_id": checkpoint["checkpoint_id"], "mission_root_handle": _ref_handle(document["mission_root"]), "strategy_root_handle": _ref_handle(document["strategy_root"]),
               "predecessor_checkpoint_id": None if document["predecessor_checkpoint"] is None else document["predecessor_checkpoint"]["checkpoint_id"],
               "counts": {field: len(sections[field]) for field in ("transitive_owner_refs", "causal_pointers", "unresolved_pointers", "pending_capture_locators")}}
    elif section == "transitive_owner_refs":
        for ref in sections[section]:
            yield {"id": _handle(str(ref["kind"]), str(ref["identity"])), "revision": int(ref["revision"]), "handle": _ref_handle(ref)}
    elif section in {"causal_pointers", "unresolved_pointers"}:
        for item in sections[section]:
            yield {"owner_handle": _ref_handle(item["owner_ref"]), "json_pointer": item["json_pointer"]}
    else:
        for item in sections[section]:
            capture_id = str(item["capture_id"])
            capture = service._store.read_raw_capture(capture_id)
            if capture["record"]["mission_id"] != service._mission_id:
                raise WorkspaceIntegrityError("Checkpoint Capture belongs to another Mission")
            ordinal = int(item["artifact_ordinal"])
            artifact = next((value for value in capture["artifacts"] if int(value["ordinal"]) == ordinal), None)
            if artifact is None:
                raise WorkspaceIntegrityError("Checkpoint artifact custody is absent")
            yield {"capture_handle": _handle("capture", capture_id), "artifact_handle": f"capture-artifact:{capture_id}#{ordinal}", "role": str(artifact["role"]), "logical_name": str(artifact["logical_name"])}


def _hooks(service: Any, selection: Mapping[str, Any], facts: Mapping[str, Any], cut: int) -> Iterator[dict[str, Any]]:
    from .executive_orientation import strategy_connections
    selected = list(_owners(service, facts, cut, {"mission", "strategy"}))
    mission = next(item for item in selected if item["reference"]["kind"] == "mission" and item["reference"]["identity"] == service._mission_id)
    strategies = set(mission["document"]["strategy_ids"])
    current = [item for item in selected if item["reference"]["kind"] == "strategy" and item["reference"]["identity"] in strategies]
    if len(current) != 1:
        raise WorkspaceIntegrityError("Root hook query requires one current Mission Strategy")
    connections = strategy_connections(current[0]["document"])
    for owner in _owners(service, facts, cut, set(selection["owner_kinds"])):
        ref = owner["reference"]
        key = (str(ref["kind"]), str(ref["identity"]), int(ref["revision"]), str(ref["payload_sha256"]))
        links = connections.get(key, [])
        if selection["strategy_relation"] == "directly_referenced" and not links or selection["strategy_relation"] == "not_directly_referenced" and links:
            continue
        # Reuse the accepted authored-field selector with a one-owner input;
        # never build global panorama or infer hooks from prose.
        buckets = service._recovery_authored_hooks({(key[0], key[1]): owner}, checkpoint_unresolved=(), candidate_a1_by_evidence={})
        projected = _semantic_document(key[0], owner["document"])
        for hook_class in selection["hook_classes"]:
            for item in buckets[hook_class]:
                value: Any = projected
                for encoded in str(item["json_pointer"])[1:].split("/"):
                    token = encoded.replace("~1", "/").replace("~0", "~")
                    value = value[int(token)] if isinstance(value, list) else value[token]
                value = deep_thaw(value)
                if selection.get("query", "").casefold() not in canonical_json_bytes(value).decode("utf-8").casefold():
                    continue
                yield {"hook_class": hook_class, "owner_handle": item["retrieval_handle"], "semantic_field": item["semantic_field"], "json_pointer": item["json_pointer"], "value": value, "strategy_connections": deep_thaw(links)}


def _filtered_captures(service: Any, selection: Mapping[str, Any], facts: Mapping[str, Any], cut: int, checkpoint: Mapping[str, Any] | None) -> Iterator[dict[str, Any]]:
    for item in _captures(service, facts, cut, checkpoint):
        if selection["view"] == "failed_interval_or_late_output" and not item["failed_interval_or_late_output"]:
            continue
        if any(key in selection and item[field] not in selection[key] for key, field in (("epoch_ids", "origin_epoch_id"), ("capture_kinds", "capture_kind"), ("origin_epoch_states", "origin_epoch_state"), ("late_classifications", "late_classification"))):
            continue
        if any(selection[key] != "any" and item[key] != selection[key] for key in ("cut_relation", "pending_state")):
            continue
        lineage = item["native_lineage"] or {}
        if "thread_ids" in selection and not set(selection["thread_ids"]) & set(lineage.values()):
            continue
        if selection.get("query", "").casefold() not in canonical_json_bytes(item).decode("utf-8").casefold():
            continue
        yield item


_NATIVE_OWNER_ORDER = (
    "branch",
    "candidate",
    "capture-annotation",
    "context",
    "evidence",
    "mission",
    "session",
    "strategy",
)
_NATIVE_HORIZON_KEYS = (*_NATIVE_OWNER_ORDER, "capture")
_NATIVE_EXAMINATION_QUANTUM = 256


def _native_position(
    value: int | str | None,
) -> tuple[Mapping[str, int] | None, Mapping[str, Any] | None]:
    if value is None:
        return None, None
    if not isinstance(value, str):
        _error(
            "mission_retrieval_cursor_invalid",
            "The native root query cursor does not carry a seek continuation.",
        )
    try:
        position = json.loads(value)
    except (TypeError, ValueError):
        _error(
            "mission_retrieval_cursor_invalid",
            "The native root query cursor has an invalid seek continuation.",
        )
    if (
        not isinstance(position, Mapping)
        or set(position) != {"horizon", "seek"}
        or not isinstance(position["horizon"], Mapping)
        or set(position["horizon"]) != set(_NATIVE_HORIZON_KEYS)
        or position["seek"] is not None
        and not isinstance(position["seek"], Mapping)
    ):
        _error(
            "mission_retrieval_cursor_invalid",
            "The native root query cursor has an invalid seek continuation.",
        )
    horizon: dict[str, int] = {}
    for key in _NATIVE_HORIZON_KEYS:
        item = position["horizon"][key]
        if isinstance(item, bool) or not isinstance(item, int) or item < 0:
            _error(
                "mission_retrieval_cursor_invalid",
                "The native root query cursor has an invalid key horizon.",
            )
        horizon[key] = item
    return horizon, position["seek"]


def _seal_native_position(
    horizon: Mapping[str, int],
    seek: Mapping[str, Any],
) -> str:
    return canonical_json_bytes(
        {"horizon": deep_thaw(horizon), "seek": deep_thaw(seek)}
    ).decode("utf-8")


def _native_owner_candidates(
    service: Any,
    *,
    kinds: set[str],
    cut: int,
    horizon: Mapping[str, int],
    after: Mapping[str, Any] | None,
) -> Iterator[tuple[Mapping[str, Any] | None, Mapping[str, Any]]]:
    requested = tuple(kind for kind in _NATIVE_OWNER_ORDER if kind in kinds)
    if not requested:
        return
    continuation = after
    while True:
        page = service._store.read_root_owner_page(
            kinds=requested,
            cut_project_commit=cut,
            horizon=horizon,
            after=continuation,
            limit=1,
        )
        scanned = page["scanned_through"]
        if scanned is None:
            return
        key = {
            "kind": str(scanned["kind"]),
            "rowid": int(scanned["rowid"]),
            "identity": str(scanned["identity"]),
        }
        eligible: Mapping[str, Any] | None = None
        for record in page["records"]:
            owner = deep_thaw(record["owner"])
            if (
                owner is not None
                and owner.get("mission_id") == service._mission_id
                and owner.get("recovery_internal") is None
            ):
                eligible = owner
        yield eligible, key
        continuation = page["next_after"]
        if continuation is None:
            return


def _native_capture_descriptor(
    service: Any,
    material: Mapping[str, Any],
    *,
    checkpoint: Mapping[str, Any] | None,
) -> dict[str, Any]:
    from .owner_content_capture import build_capture_descriptor

    return build_capture_descriptor(material, checkpoint=checkpoint)


def _native_capture_candidates(
    service: Any,
    *,
    cut: int,
    checkpoint: Mapping[str, Any] | None,
    horizon: Mapping[str, int],
    after: Mapping[str, Any] | None,
) -> Iterator[tuple[dict[str, Any] | None, Mapping[str, Any]]]:
    continuation = after
    while True:
        page = service._store.read_root_capture_page(
            mission_id=service._mission_id,
            cut_project_commit=cut,
            horizon=horizon,
            after=continuation,
            limit=1,
        )
        scanned = page["scanned_through"]
        if scanned is None:
            return
        descriptor = None
        for material in page["records"]:
            descriptor = _native_capture_descriptor(
                service,
                material,
                checkpoint=checkpoint,
            )
        key = {"rowid": int(scanned["rowid"]), "identity": str(scanned["identity"])}
        yield descriptor, key
        continuation = page["next_after"]
        if continuation is None:
            return


def _native_capture_matches(
    selection: Mapping[str, Any],
    item: Mapping[str, Any],
) -> bool:
    if (
        selection["view"] == "failed_interval_or_late_output"
        and not item["failed_interval_or_late_output"]
    ):
        return False
    if any(
        key in selection and item[field] not in selection[key]
        for key, field in (
            ("epoch_ids", "origin_epoch_id"),
            ("capture_kinds", "capture_kind"),
            ("origin_epoch_states", "origin_epoch_state"),
            ("late_classifications", "late_classification"),
        )
    ):
        return False
    if any(
        selection[key] != "any" and item[key] != selection[key]
        for key in ("cut_relation", "pending_state")
    ):
        return False
    lineage = item["native_lineage"] or {}
    if "thread_ids" in selection and not set(selection["thread_ids"]) & set(
        lineage.values()
    ):
        return False
    return selection.get("query", "").casefold() in canonical_json_bytes(
        item
    ).decode("utf-8").casefold()


def _native_discovery(
    service: Any,
    selection: Mapping[str, Any],
    *,
    cut: int,
    checkpoint: Mapping[str, Any] | None,
    horizon: Mapping[str, int],
    position: Mapping[str, Any] | None,
) -> Iterator[tuple[dict[str, Any] | None, Mapping[str, Any]]]:
    if position is not None and (
        set(position) != {"phase", "after"}
        or position["phase"] not in {"owners", "captures"}
    ):
        _error(
            "mission_retrieval_cursor_invalid",
            "The root discovery cursor has an invalid seek continuation.",
        )
    kinds = set(selection["kinds"])
    phase = "owners" if position is None else str(position["phase"])
    if phase == "owners":
        after = None if position is None else position["after"]
        if after is not None and (
            not isinstance(after, Mapping)
            or set(after) != {"kind", "rowid", "identity"}
            or after["kind"] not in kinds
            or isinstance(after["rowid"], bool)
            or not isinstance(after["rowid"], int)
            or after["rowid"] < 1
            or not isinstance(after["identity"], str)
            or not after["identity"]
        ):
            _error(
                "mission_retrieval_cursor_invalid",
                "The root discovery owner continuation is invalid.",
            )
        for owner, key in _native_owner_candidates(
            service,
            kinds=kinds,
            cut=cut,
            horizon=horizon,
            after=after,
        ):
            if owner is None:
                yield None, {"phase": "owners", "after": key}
                continue
            ref = owner["reference"]
            item = {
                "id": _ref_handle(ref),
                "kind": str(ref["kind"]),
                "title": str(ref["identity"]),
                "completeness": "descriptor",
                "trust_class": "semantic_owner_descriptor",
            }
            yield item, {"phase": "owners", "after": key}
    if "capture" not in kinds:
        return
    capture_after = (
        None
        if phase == "owners"
        else position["after"] if position is not None else None
    )
    if capture_after is not None and (
        not isinstance(capture_after, Mapping)
        or set(capture_after) != {"rowid", "identity"}
        or isinstance(capture_after["rowid"], bool)
        or not isinstance(capture_after["rowid"], int)
        or capture_after["rowid"] < 1
        or not isinstance(capture_after["identity"], str)
        or not capture_after["identity"]
    ):
        _error(
            "mission_retrieval_cursor_invalid",
            "The root discovery Capture continuation is invalid.",
        )
    for item, capture_id in _native_capture_candidates(
        service,
        cut=cut,
        checkpoint=checkpoint,
        horizon=horizon,
        after=capture_after,
    ):
        if item is None:
            yield None, {"phase": "captures", "after": capture_id}
            continue
        yield item, {"phase": "captures", "after": capture_id}


def _native_changes(
    service: Any,
    selection: Mapping[str, Any],
    *,
    cut: int,
    baseline: int,
    horizon: Mapping[str, int],
    position: Mapping[str, Any] | None,
) -> Iterator[tuple[dict[str, Any] | None, Mapping[str, Any]]]:
    """Compare bounded current owner pages at the checkpoint and fixed cut."""

    if position is not None and (
        set(position) != {"phase", "after"}
        or position["phase"] != "changes"
        or not isinstance(position["after"], Mapping)
        or set(position["after"]) != {"kind", "rowid", "identity"}
        or position["after"].get("kind") not in set(selection["kinds"])
        or isinstance(position["after"].get("rowid"), bool)
        or not isinstance(position["after"].get("rowid"), int)
        or position["after"]["rowid"] < 1
        or not isinstance(position["after"].get("identity"), str)
        or not position["after"]["identity"]
    ):
        _error(
            "mission_retrieval_cursor_invalid",
            "The root change cursor has an invalid seek continuation.",
        )
    continuation = None if position is None else position["after"]
    while True:
        page = service._store.read_root_change_page(
            mission_id=service._mission_id,
            baseline_project_commit=baseline,
            cut_project_commit=cut,
            kinds=tuple(selection["kinds"]),
            relations=tuple(selection["relations"]),
            horizon=horizon,
            after=continuation,
            limit=1,
        )
        emitted = False
        for record in page["records"]:
            item = deep_thaw(record)
            record_position = item.pop("position")
            yield item, {"phase": "changes", "after": record_position}
            emitted = True
        if not emitted and page["scanned_through"] is not None:
            yield None, {
                "phase": "changes",
                "after": deep_thaw(page["scanned_through"]),
            }
        if page["exhausted"]:
            return
        continuation = page["next_after"]
        if continuation is None:
            raise WorkspaceIntegrityError(
                "native root change page lost its seek continuation"
            )


def _native_hooks(
    service: Any,
    selection: Mapping[str, Any],
    *,
    cut: int,
    horizon: Mapping[str, int],
    position: Mapping[str, Any] | None,
) -> Iterator[tuple[dict[str, Any] | None, Mapping[str, Any]]]:
    """Seek authenticated hook items without reopening an active owner."""

    def valid_reference(value: Any, *, kind: str | None = None) -> bool:
        return (
            isinstance(value, Mapping)
            and set(value) == {"kind", "identity", "revision", "payload_sha256"}
            and (kind is None or value.get("kind") == kind)
            and isinstance(value.get("kind"), str)
            and isinstance(value.get("identity"), str)
            and bool(value.get("identity"))
            and not isinstance(value.get("revision"), bool)
            and isinstance(value.get("revision"), int)
            and value["revision"] >= 1
            and isinstance(value.get("payload_sha256"), str)
            and re.fullmatch(r"[0-9a-f]{64}", value["payload_sha256"]) is not None
        )

    hook_fields = {
        field
        for specifications in _ROOT_HOOK_FIELD_CLASSES.values()
        for _hook_class, field in specifications
    }

    def valid_descriptor(value: Any, *, family: str) -> bool:
        keys = {
            "owner_reference",
            "origin_project_commit",
            "root_digest",
            "entry_count",
        }
        if family == "hook_item":
            keys.add("field_counts")
        return (
            isinstance(value, Mapping)
            and set(value) == keys
            and valid_reference(
                value.get("owner_reference"),
                kind="strategy" if family == "strategy_connection" else None,
            )
            and isinstance(value.get("root_digest"), str)
            and re.fullmatch(r"[0-9a-f]{64}", value["root_digest"]) is not None
            and not isinstance(value.get("origin_project_commit"), bool)
            and isinstance(value.get("origin_project_commit"), int)
            and 1 <= value["origin_project_commit"] <= cut
            and not isinstance(value.get("entry_count"), bool)
            and isinstance(value.get("entry_count"), int)
            and value["entry_count"] >= 0
            and (
                family != "hook_item"
                or isinstance(value.get("field_counts"), Mapping)
                and set(value["field_counts"]) == hook_fields
                and all(
                    not isinstance(count, bool)
                    and isinstance(count, int)
                    and count >= 0
                    for count in value["field_counts"].values()
                )
                and sum(value["field_counts"].values()) == value["entry_count"]
            )
        )

    def valid_owner_key(value: Any) -> bool:
        return (
            isinstance(value, Mapping)
            and set(value) == {"kind", "rowid", "identity"}
            and value.get("kind") in set(selection["owner_kinds"])
            and not isinstance(value.get("rowid"), bool)
            and isinstance(value.get("rowid"), int)
            and value["rowid"] >= 1
            and isinstance(value.get("identity"), str)
            and bool(value.get("identity"))
        )

    if position is not None and (
        not isinstance(position, Mapping)
        or set(position) != {"phase", "after", "hook_after", "strategy"}
        or position.get("phase") != "hooks"
    ):
        _error(
            "mission_retrieval_cursor_invalid",
            "The root hook cursor has an invalid seek continuation.",
        )
    after = None if position is None else position["after"]
    hook_after = None if position is None else position["hook_after"]
    strategy = (
        deep_thaw(
            service._store.read_root_hook_strategy_descriptor(
                mission_id=service._mission_id,
                cut_project_commit=cut,
            )
        )
        if position is None
        else position["strategy"]
    )
    if after is not None and not valid_owner_key(after):
        _error(
            "mission_retrieval_cursor_invalid",
            "The root hook owner continuation is invalid.",
        )
    if (
        not isinstance(strategy, Mapping)
        or set(strategy) != {"mission_reference", "connection_index"}
        or not valid_reference(strategy.get("mission_reference"), kind="mission")
        or strategy["mission_reference"].get("identity") != service._mission_id
        or not valid_descriptor(
            strategy.get("connection_index"), family="strategy_connection"
        )
    ):
        _error(
            "mission_retrieval_cursor_invalid",
            "The root hook Strategy continuation is invalid.",
        )
    if hook_after is not None and (
        not isinstance(hook_after, Mapping)
        or set(hook_after)
        != {"owner", "descriptor", "hook_class", "semantic_field", "item_index"}
        or not valid_owner_key(hook_after.get("owner"))
        or not valid_descriptor(hook_after.get("descriptor"), family="hook_item")
        or hook_after["descriptor"]["owner_reference"].get("kind")
        != hook_after["owner"].get("kind")
        or hook_after["descriptor"]["owner_reference"].get("identity")
        != hook_after["owner"].get("identity")
        or hook_after.get("hook_class") not in set(selection["hook_classes"])
        or not isinstance(hook_after.get("semantic_field"), str)
        or isinstance(hook_after.get("item_index"), bool)
        or not isinstance(hook_after.get("item_index"), int)
        or hook_after["item_index"] < -1
    ):
        _error(
            "mission_retrieval_cursor_invalid",
            "The root hook item continuation is invalid.",
        )

    def owner_rows(
        *, continuation: Mapping[str, Any] | None,
    ) -> Iterator[tuple[Mapping[str, Any] | None, Mapping[str, Any]]]:
        requested = tuple(
            kind for kind in _NATIVE_OWNER_ORDER
            if kind in set(selection["owner_kinds"])
            and kind in _ROOT_HOOK_FIELD_CLASSES
        )
        while requested:
            page = service._store.read_root_owner_page(
                kinds=requested,
                cut_project_commit=cut,
                horizon=horizon,
                after=continuation,
                limit=1,
                include_hook_index=True,
            )
            scanned = page["scanned_through"]
            if scanned is None:
                return
            key = {
                "kind": str(scanned["kind"]),
                "rowid": int(scanned["rowid"]),
                "identity": str(scanned["identity"]),
            }
            selected = None
            for record in page["records"]:
                owner = deep_thaw(record["owner"])
                if (
                    owner.get("mission_id") == service._mission_id
                    and owner.get("recovery_internal") is None
                    and "hook_index" in record
                ):
                    selected = {
                        "owner": owner,
                        "descriptor": deep_thaw(record["hook_index"]),
                    }
            yield selected, key
            continuation = page["next_after"]
            if continuation is None:
                return

    query = selection.get("query", "").casefold()

    def project_owner(
        *, key: Mapping[str, Any], descriptor: Mapping[str, Any],
        start: tuple[str, str, int] | None,
    ) -> Iterator[tuple[dict[str, Any] | None, Mapping[str, Any]]]:
        reference = descriptor["owner_reference"]
        owner_kind = str(reference["kind"])
        specs = tuple(
            (hook_class, field)
            for hook_class in selection["hook_classes"]
            for candidate_class, field in _ROOT_HOOK_FIELD_CLASSES.get(
                owner_kind, ()
            )
            if candidate_class == hook_class
        )
        start_spec, start_index = 0, 0
        if start is not None:
            target = (start[0], start[1])
            try:
                start_spec = specs.index(target)
            except ValueError:
                _error(
                    "mission_retrieval_cursor_invalid",
                    "The root hook continuation selected an unavailable field.",
                )
            field_count = int(descriptor["field_counts"][target[1]])
            if start[2] >= field_count:
                _error(
                    "mission_retrieval_cursor_invalid",
                    "The root hook continuation lies beyond its selected field.",
                )
            start_index = start[2] + 1

        links = service._store.read_root_strategy_connections(
            descriptor=strategy["connection_index"],
            owner_reference=reference,
        )
        relation_excluded = (
            selection["strategy_relation"] == "directly_referenced" and not links
        ) or (
            selection["strategy_relation"] == "not_directly_referenced" and links
        )
        if relation_excluded:
            if start is not None:
                _error(
                    "mission_retrieval_cursor_invalid",
                    "The root hook continuation no longer matches its Strategy relation.",
                )
            return

        if start is None:
            first = next(
                (
                    (hook_class, field)
                    for hook_class, field in specs
                    if int(descriptor["field_counts"][field]) > 0
                ),
                None,
            )
            if first is not None:
                yield None, {
                    "phase": "hooks",
                    "after": after,
                    "hook_after": {
                        "owner": deep_thaw(key),
                        "descriptor": deep_thaw(descriptor),
                        "hook_class": first[0],
                        "semantic_field": first[1],
                        "item_index": -1,
                    },
                    "strategy": deep_thaw(strategy),
                }

        for spec_ordinal, (hook_class, field) in enumerate(
            specs[start_spec:], start=start_spec
        ):
            index_start = start_index if spec_ordinal == start_spec else 0
            for item_index in range(
                index_start, int(descriptor["field_counts"][field])
            ):
                value = deep_thaw(
                    service._store.read_root_hook_item(
                        descriptor=descriptor,
                        hook_class=hook_class,
                        semantic_field=field,
                        item_index=item_index,
                    )
                )
                source_item_index = item_index
                if owner_kind == "candidate" and field == "genealogy":
                    if (
                        not isinstance(value, Mapping)
                        or set(value) != {"source_item_index", "authored_value"}
                        or isinstance(value["source_item_index"], bool)
                        or not isinstance(value["source_item_index"], int)
                        or value["source_item_index"] < 0
                        or not isinstance(value["authored_value"], Mapping)
                        or value["authored_value"].get("relation") != "recombines"
                    ):
                        raise WorkspaceIntegrityError(
                            "Candidate recombination hook index value is invalid"
                        )
                    source_item_index = int(value["source_item_index"])
                    value = deep_thaw(value["authored_value"])
                row = None
                if query in canonical_json_bytes(value).decode(
                    "utf-8"
                ).casefold():
                    row = {
                        "hook_class": hook_class,
                        "owner_handle": (
                            f"{owner_kind}:{reference['identity']}@"
                            f"{reference['revision']}"
                        ),
                        "semantic_field": field,
                        "json_pointer": f"/{field}/{source_item_index}",
                        "value": value,
                        "strategy_connections": deep_thaw(links),
                    }
                yield row, {
                    "phase": "hooks",
                    "after": after,
                    "hook_after": {
                        "owner": deep_thaw(key),
                        "descriptor": deep_thaw(descriptor),
                        "hook_class": hook_class,
                        "semantic_field": field,
                        "item_index": item_index,
                    },
                    "strategy": deep_thaw(strategy),
                }

    if hook_after is not None:
        if dict(hook_after["owner"]) == after:
            _error(
                "mission_retrieval_cursor_invalid",
                "The root hook continuation already consumed its owner.",
            )
        yield from project_owner(
            key=hook_after["owner"],
            descriptor=hook_after["descriptor"],
            start=(
                str(hook_after["hook_class"]),
                str(hook_after["semantic_field"]),
                int(hook_after["item_index"]),
            ),
        )
        after = hook_after["owner"]
        yield None, {
            "phase": "hooks", "after": deep_thaw(after),
            "hook_after": None, "strategy": deep_thaw(strategy),
        }

    for selected, key in owner_rows(continuation=after):
        if selected is not None:
            descriptor = selected["descriptor"]
            if not valid_descriptor(descriptor, family="hook_item"):
                raise WorkspaceIntegrityError(
                    "root hook owner lost its derived index commitment"
                )
            yield from project_owner(
                key=key, descriptor=descriptor, start=None,
            )
        after = key
        yield None, {
            "phase": "hooks", "after": deep_thaw(after),
            "hook_after": None, "strategy": deep_thaw(strategy),
        }

_RESEARCH_KIND_FAMILIES = {
    **{kind: family for family, kind in _FAMILIES.items()},
    "mission": "missions", "session": "sessions",
    "capture": "captures", "capture-artifact": "capture_artifacts",
}


def _research_selected_context(service: Any, selection: Mapping[str, Any], *, cut: int) -> dict[str, Any]:
    from .context_revision import context_reference_projection, validate_context_source_selection
    from .executive_orientation import source_qualification_document

    kind, identity, revision = service._parse_retrieval_id(str(selection["id"]))
    owner = service._store.read_indexed_observation_owner(
        mission_id=service._mission_id, kind=kind, identity=identity,
        revision=revision, source_project_commit=cut,
    )
    if owner is None or owner["owner"]["mission_id"] != service._mission_id:
        _error("research_read_material_unavailable", "The selected Context revision is not available in this Mission at the read cut.")
    document = owner["owner"]["document"]
    chosen = selection["selection"]
    try:
        validate_context_source_selection(owner["owner"]["reference"], chosen)
        if document.get("schema_version") != 3:
            raise ValueError("selected scientific Context reads require a v3 Context")
        # This owner validates the complete Context and every selected key. The
        # qualification projector may omit missing keys for its partial-view
        # caller, so it must not be used alone for an exact requested read.
        context_reference_projection(
            document, purpose="discovery",
            treatment_ids=chosen.get("treatment_ids"),
            whole_context=chosen["mode"] == "whole_context",
        )
    except (ValueError, TypeError) as exc:
        _error("research_read_selection_invalid", str(exc))
    item = {"requested_id": selection["id"], "status": "readable",
            "id": _ref_handle(owner["owner"]["reference"]), "kind": "context", "title": identity,
            "readable_content": canonical_json_bytes(source_qualification_document("context", document, chosen)).decode("utf-8"),
            "completeness": "complete_declared_semantic_projection", "trust_class": "semantic_owner_projection"}
    return {"mode": "selected_context", "purpose": selection["purpose"], "items": [item], "next_cursor": None}


def research_retrieve(
    service: Any, selection: Mapping[str, Any], *, grant: Mapping[str, Any],
    research_query_context: Mapping[str, Any],
) -> tuple[dict[str, Any], int]:
    """Read within the caller's held snapshot; never enter the root console.

    The separate public family boundary checks live authority first. Shared
    readers keep their ordinary custody/index semantics; the private outer MAC
    binds current-query continuation to this child, grant, assignment and cut.
    """
    from .mission_operation_contract import HISTORICAL_RETRIEVE_MODES, HISTORICAL_SOURCE_FAMILIES, RETRIEVE

    if (not isinstance(research_query_context, Mapping)
        or set(research_query_context) != {"cursor_mac_key"}
        or not isinstance(research_query_context["cursor_mac_key"], str)
        or re.fullmatch(r"[0-9a-f]{64}", research_query_context["cursor_mac_key"]) is None):
        _error("research_read_context_invalid", "Research reads require their private Host signing context.")
    signing_id = "research-query:" + research_query_context["cursor_mac_key"]
    current = service._store.read_root_retrieval_cut()
    cut = int(current["project_commit"])
    query = {key: deep_thaw(value) for key, value in selection.items() if key != "cursor"}
    mode = str(query["mode"])
    families = set(grant["source_families"])
    if mode == "search":
        permitted = [kind for kind in (*OWNER_KINDS, "capture", "capture-annotation")
                     if _RESEARCH_KIND_FAMILIES[kind] in families]
        query.setdefault("kinds", permitted)
        if not query["kinds"] or set(query["kinds"]) - set(permitted):
            _error("research_read_scope_denied", "Search kinds exceed the granted scientific source families.")
        query = _normalized(query)
    elif mode in HISTORICAL_RETRIEVE_MODES:
        permitted = [family for family in HISTORICAL_SOURCE_FAMILIES if family in families]
        query.setdefault("source_families", permitted)
        if not query["source_families"] or set(query["source_families"]) - set(permitted):
            _error("research_read_scope_denied", "Historical selection exceeds the granted source families.")
    handles = query.get("ids", [query["id"]] if mode == "selected_context" else [])
    for handle in handles:
        kind, _, _ = service._parse_retrieval_id(str(handle))
        if _RESEARCH_KIND_FAMILIES.get(kind) not in families:
            _error("research_read_scope_denied", "The exact requested owner is outside the granted source families.")
        if kind == "capture-artifact" and mode == "read" and grant["raw_body_policy"] != "allow_untrusted_material":
            _error("research_read_raw_body_denied", "This grant authorizes capture metadata only.")
    scope = {"schema_version": "mathematical_research.research_read_cursor_scope.v1",
             "grant_id": grant["grant_id"], "assignment_id": grant["assignment_id"],
             "project_id": grant["project_id"], "mission_id": grant["mission_id"],
             "executive_epoch_id": grant["executive_epoch_id"],
             "root_thread_id": grant["root_thread_id"], "child_thread_id": grant["child_thread_id"],
             "parent_thread_id": grant["parent_thread_id"], "query": query}
    continuation = None
    if selection.get("cursor") is not None:
        try:
            decoded = json.loads(service._store.read_historical_read_cursor(
                str(selection["cursor"]), grant_id=signing_id, scope=scope,
            ))
            if (not isinstance(decoded, dict) or set(decoded) != {"cut", "cut_binding", "position"}
                or type(decoded["cut"]) is not int or not 0 <= decoded["cut"] <= cut):
                raise ValueError("invalid research continuation")
            cut = decoded["cut"]
            fixed = service._store.read_root_retrieval_cut(cut_project_commit=cut)
            if decoded["cut_binding"] != {key: fixed[key] for key in ("root_digest", "transition_head_digest", "canonical_authority_digest")}:
                raise ValueError("research source cut changed")
            continuation = decoded["position"]
        except (ValueError, TypeError, KeyError, UnicodeError) as exc:
            _error("research_read_cursor_invalid", "Research continuation no longer matches this exact live grant/query/cut.")
    if mode == "read":
        indexed_cut = cut if service._store.read_metadata()["schema_version"] == 12 else None
        result = {"mode": mode, "purpose": query["purpose"],
                  "scope": {"basis": "exact_selection", "checkpoint_id": None, "selection": query},
                  "completeness": "exhausted", "state": "present",
                  "items": [_exact_read(service, handle, source_project_commit=indexed_cut) for handle in query["ids"]],
                  "next_cursor": None}
    elif mode == "selected_context":
        result = _research_selected_context(service, query, cut=cut)
    elif mode == "search":
        page, continuation, more = _indexed_search_page(service, query, cut=cut, position=continuation)
        result = {"mode": mode, "purpose": query["purpose"],
                  "scope": {"basis": "current_at_first_page", "checkpoint_id": None, "selection": query},
                  "completeness": "page" if more else "exhausted", "state": "present" if page else "none",
                  "items": page, "next_cursor": None}
    else:
        historical_query = deep_thaw(query)
        if continuation is not None:
            historical_query["cursor"] = continuation
        historical = service._execute_scientific_history_read(
            {"operation": RETRIEVE, "input": historical_query},
            validated_grant={**deep_thaw(grant), "source_families": [family for family in HISTORICAL_SOURCE_FAMILIES if family in families],
                             "project_commit_cut": cut}, ordinary_research=True,
        )
        result = deep_thaw(historical["result"])
        continuation = result["next_cursor"]
        result["next_cursor"] = None
    if continuation is not None:
        fixed = service._store.read_root_retrieval_cut(cut_project_commit=cut)
        result["next_cursor"] = service._store.issue_historical_read_cursor(
            grant_id=signing_id, scope=scope,
            position=canonical_json_bytes({"cut": cut, "cut_binding": {
                key: fixed[key] for key in ("root_digest", "transition_head_digest", "canonical_authority_digest")
            }, "position": continuation}).decode("utf-8"),
        )
    return result, cut


def root_retrieve(service: Any, selection: Mapping[str, Any], *, executive_epoch_id: str, root_query_context: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """Execute a validated model query under one read snapshot, with live authority."""
    mode = str(selection["mode"])
    if root_query_context is not None and (not isinstance(root_query_context, Mapping) or set(root_query_context) != {"cursor_mac_key"} or not isinstance(root_query_context["cursor_mac_key"], str) or re.fullmatch(r"[0-9a-f]{64}", root_query_context["cursor_mac_key"]) is None):
        _error("mission_retrieval_context_invalid", "The private root query context is invalid.")
    key = None if root_query_context is None else str(root_query_context["cursor_mac_key"])
    with service._store.direct_recovery_read_scope():
        cut_profile = service._store.read_root_retrieval_cut()
        mission = service._require_mission()
        if mission["lifecycle"] != "active" or mission["effective"] is not True:
            _error(
                "mission_interface_mission_fenced",
                "Mission is fenced or no longer effective.",
            )
        authority = service._direct_epoch_authority(
            executive_epoch_id,
            root_retrieval_cut=cut_profile,
        )
        if authority is None:
            _error("executive_epoch_unavailable", "No current Executive Epoch authorizes retrieval.")
        if mode == "publication_result":
            result = service._store.read_scientific_publication_result(
                mission_id=service._mission_id, command_id=str(selection["command_id"]),
            )
            if result is None:
                raise StaleCommandError("scientific publication result is absent from this Mission")
            return deep_thaw(result)
        if mode == "read":
            indexed_cut = (
                int(cut_profile["project_commit"])
                if service._store.read_metadata()["schema_version"] == 12
                else None
            )
            return {"mode": mode, "purpose": selection["purpose"], "scope": {"basis": "exact_selection", "checkpoint_id": None, "selection": deep_thaw(selection)}, "completeness": "exhausted", "state": "present", "items": [_exact_read(service, value, source_project_commit=indexed_cut) for value in selection["ids"]], "next_cursor": None}
        query = _normalized(selection)
        cut = int(cut_profile["project_commit"])
        checkpoint = _checkpoint(service, selection.get("checkpoint_id"))
        checkpoint_id = None if checkpoint is None else str(checkpoint["checkpoint_id"])
        cursor = selection.get("cursor")
        token = None
        if cursor is not None:
            if key is None:
                _error("mission_retrieval_context_required", "Root pagination requires its private Host signing context.")
            try:
                envelope = json.loads(base64.urlsafe_b64decode(str(cursor) + "=" * (-len(str(cursor)) % 4)))
                if set(envelope) != {"cut", "checkpoint_id", "token"} or type(envelope["cut"]) is not int or not 0 <= envelope["cut"] <= cut:
                    raise ValueError()
                cut = envelope["cut"]
                checkpoint_id = envelope["checkpoint_id"]
                if checkpoint_id is not None and not isinstance(checkpoint_id, str):
                    raise ValueError()
                checkpoint = None if checkpoint_id is None else _checkpoint(service, checkpoint_id)
                token = envelope["token"]
            except (ValueError, TypeError, KeyError, UnicodeError):
                _error("mission_retrieval_cursor_invalid", "The root query cursor is malformed or stale; refresh this query.")
            cut_profile = service._store.read_root_retrieval_cut(
                cut_project_commit=cut
            )
        basis = "immutable_checkpoint" if mode == "checkpoint" else "current_at_first_page"
        proof = None
        proof_fingerprint = None
        if mode == "proof_attention":
            from .executive_orientation import proof_attention_in_snapshot
            proof = proof_attention_in_snapshot(service)
            proof_fingerprint = hashlib.sha256(canonical_json_bytes(proof)).hexdigest()
            basis = "live_proof_attention"
        store_route = str(cut_profile["route"])
        if mode == "search":
            cursor_route = "authenticated_owner_content_index"
        elif mode == "checkpoint":
            cursor_route = (
                "checkpoint_compact_direct"
                if checkpoint is not None
                and checkpoint["document"].get("schema_version") == 2
                and query["section"] == "unresolved_pointers"
                else "checkpoint_explicit_materialization"
            )
        elif mode == "proof_attention":
            cursor_route = "live_proof_projection"
        else:
            cursor_route = store_route
        scope = {"mission_id": service._mission_id, "epoch_id": authority.executive_epoch_id, "root_thread_id": authority.goal_thread_id,
                 "bound_event_digest": authority.bound_event_digest, "mission_root": deep_thaw(authority.mission_root),
                 "query": query, "cut": cut, "cut_binding": {
                     "root_digest": cut_profile["root_digest"],
                     "transition_head_digest": cut_profile["transition_head_digest"],
                     "canonical_authority_digest": cut_profile["canonical_authority_digest"],
                 }, "route": cursor_route, "checkpoint_id": checkpoint_id, "proof_fingerprint": proof_fingerprint}
        position: int | str | None = None
        if token is not None:
            try:
                position = service._store.read_historical_read_cursor(token, grant_id="root-query:" + key, scope=scope)
            except (ValueError, TypeError):
                _error("mission_retrieval_cursor_invalid", "The root query cursor no longer matches this exact query, authority or signing generation.")
        native = store_route == "native_root6_indexed"
        native_modes = {
            "inventory",
            "changes_since_checkpoint",
            "hooks",
            "captures",
        }
        page: list[dict[str, Any]] = []
        more = False
        next_position: int | str | None = None
        if mode == "search":
            page, next_position, more = _indexed_search_page(
                service, query, cut=cut, position=position,
            )
        elif native and mode in native_modes:
            horizon, seek = _native_position(position)
            if horizon is None:
                horizon = deep_thaw(
                    service._store.read_root_retrieval_horizon(
                        cut_project_commit=cut
                    )
                )
            if mode == "inventory":
                native_rows = _native_discovery(
                    service,
                    query,
                    cut=cut,
                    checkpoint=checkpoint,
                    horizon=horizon,
                    position=seek,
                )
            elif mode == "changes_since_checkpoint":
                native_rows = iter(()) if checkpoint is None else _native_changes(
                    service,
                    query,
                    cut=cut,
                    baseline=int(checkpoint["project_commit_no"]),
                    horizon=horizon,
                    position=seek,
                )
            elif mode == "hooks":
                native_rows = _native_hooks(
                    service,
                    query,
                    cut=cut,
                    horizon=horizon,
                    position=seek,
                )
            else:
                if seek is not None and (
                    set(seek) != {"phase", "after"}
                    or seek["phase"] != "captures"
                    or not isinstance(seek["after"], Mapping)
                    or set(seek["after"]) != {"rowid", "identity"}
                    or isinstance(seek["after"].get("rowid"), bool)
                    or not isinstance(seek["after"].get("rowid"), int)
                    or seek["after"]["rowid"] < 1
                    or not isinstance(seek["after"].get("identity"), str)
                    or not seek["after"]["identity"]
                ):
                    _error(
                        "mission_retrieval_cursor_invalid",
                        "The root Capture cursor has an invalid seek continuation.",
                    )
                native_rows = (
                    (
                        item
                        if item is not None and _native_capture_matches(query, item)
                        else None,
                        {"phase": "captures", "after": capture_position},
                    )
                    for item, capture_position in _native_capture_candidates(
                        service,
                        cut=cut,
                        checkpoint=checkpoint,
                        horizon=horizon,
                        after=None if seek is None else seek["after"],
                    )
                )
            last_examined = seek
            examination_budget = _NATIVE_EXAMINATION_QUANTUM
            examined = 0
            rows = iter(native_rows)
            while examined < examination_budget:
                try:
                    row, row_position = next(rows)
                except StopIteration:
                    break
                examined += 1
                if row is not None and len(page) == query["page_size"]:
                    if last_examined is None:
                        raise WorkspaceIntegrityError(
                            "native root pagination lost its pre-match seek position"
                        )
                    next_position = _seal_native_position(horizon, last_examined)
                    more = True
                    break
                last_examined = row_position
                if row is not None:
                    page.append(deep_thaw(row))
            else:
                try:
                    row, row_position = next(rows)
                except StopIteration:
                    pass
                else:
                    if last_examined is None:
                        raise WorkspaceIntegrityError(
                            "native root pagination lost its examination seek position"
                        )
                    next_position = _seal_native_position(horizon, last_examined)
                    more = True
        else:
            if position is None:
                ordinal_position = 0
            elif isinstance(position, int) and not isinstance(position, bool):
                ordinal_position = position
            else:
                _error(
                    "mission_retrieval_cursor_invalid",
                    "The exhaustive root query cursor has an invalid position.",
                )
            if mode == "inventory":
                facts = service._store.read_direct_recovery_facts(
                    cut_project_commit=None
                )
                rows = _discovery(service, query, facts, cut, checkpoint)
            elif mode == "checkpoint":
                rows = _checkpoint_items(service, checkpoint, str(query["section"]))
            elif mode == "changes_since_checkpoint":
                facts = service._store.read_direct_recovery_facts(
                    cut_project_commit=None
                )
                changes = [] if checkpoint is None else change_descriptors_in_snapshot(service, facts, cut=cut, baseline=int(checkpoint["project_commit_no"]))
                rows = (row for row in changes if row["kind"] in query["kinds"] and set(row["changes"]) & set(query["relations"]))
            elif mode == "hooks":
                facts = service._store.read_direct_recovery_facts(
                    cut_project_commit=None
                )
                rows = _hooks(service, query, facts, cut)
            elif mode == "captures":
                facts = service._store.read_direct_recovery_facts(
                    cut_project_commit=None
                )
                rows = _filtered_captures(service, query, facts, cut, checkpoint)
            else:
                rows = iter(proof["open_candidate_a1"])
            for index, row in enumerate(rows):
                if index < ordinal_position:
                    continue
                if len(page) == query["page_size"]:
                    more = True
                    break
                page.append(deep_thaw(row))
            if more:
                next_position = ordinal_position + len(page)
        next_cursor = None
        if more:
            if key is None:
                _error("mission_retrieval_context_required", "Root pagination requires its private Host signing context.")
            if next_position is None:
                raise WorkspaceIntegrityError(
                    "root query pagination lost its exact continuation"
                )
            sealed = service._store.issue_historical_read_cursor(grant_id="root-query:" + key, scope=scope, position=next_position)
            next_cursor = base64.urlsafe_b64encode(canonical_json_bytes({"cut": cut, "checkpoint_id": checkpoint_id, "token": sealed})).decode("ascii").rstrip("=")
        result = {"mode": mode, "purpose": query["purpose"], "scope": {"basis": basis, "checkpoint_id": checkpoint_id, "selection": query},
                  "completeness": "page" if more else "exhausted", "state": "no_checkpoint" if checkpoint is None and mode in {"checkpoint", "changes_since_checkpoint"} else "present" if page else "none",
                  "items": page, "next_cursor": next_cursor}
        if proof is not None:
            result["admitted_result"] = deep_thaw(proof["admitted_result"])
        return result
