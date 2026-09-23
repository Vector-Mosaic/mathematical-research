"""Private operator observation, composed from existing read owners.

The fixed transport authenticates its operator before every call and injects a
process-private cursor key. This facade has no Executive, child grant, lease,
mutation, or generic file-read route. Each call holds one read snapshot; cursors
retain identities, not a transaction or a copy of research memory.
"""
from __future__ import annotations

import base64
import hashlib
import json
import re
from collections.abc import Mapping
from typing import Any

from .candidate_revision import candidate_a1_requirement
from .executive_orientation import semantic_owner_document, strategy_connections
from .json_support import canonical_json_bytes
from .mission_retrieval import _ref_handle, evidence_source_navigation
from .research_model import deep_thaw
from .workspace_store import RawCaptureArtifactUnavailableError, StaleCommandError, WorkspaceIntegrityError

SCHEMA = "mathematical_research.mission_observation.v1"
OPERATIONS = {"current_decision", "decision_history", "exact_record", "catalog"}
COLLECTIONS = {"frontier", "owner_records", "open_candidates", "latest_outputs", "relationship_hooks"}
OWNER_KINDS = {"mission", "strategy", "branch", "context", "evidence", "candidate"}
_BINDING_KEYS = {"project_id", "mission_id", "root_identity", "project_commit", "root_digest", "transition_head_digest", "canonical_authority_digest"}


class _Unavailable(Exception):
    pass


class _Denied(Exception):
    pass


class _Expired(Exception):
    pass


def validate_observation_request(value: Mapping[str, Any]) -> dict[str, Any]:
    """Validate the public shape before transport opens any Workspace."""
    if not isinstance(value, Mapping):
        raise ValueError("observation request must be an object")
    request = deep_thaw(value)
    operation = request.get("operation")
    if not isinstance(operation, str) or operation not in OPERATIONS:
        raise ValueError("unsupported observation operation")
    allowed = {"operation", "source_binding"}
    if operation in {"catalog", "decision_history"}:
        allowed |= {"collection", "page_size", "cursor"}
        collections = COLLECTIONS if operation == "catalog" else {"strategies", "checkpoints"}
        if not isinstance(request.get("collection"), str) or request["collection"] not in collections:
            raise ValueError("unsupported observation collection")
        request.setdefault("page_size", 25)
        if type(request["page_size"]) is not int or not 1 <= request["page_size"] <= 50:
            raise ValueError("observation page size must be 1..50")
        if "cursor" in request and (not isinstance(request["cursor"], str) or not 1 <= len(request["cursor"]) <= 32768):
            raise ValueError("observation cursor is malformed")
    elif operation == "exact_record":
        allowed |= {"handle", "source_evidence", "source_ordinal", "offset_bytes", "max_bytes"}
        if not isinstance(request.get("handle"), str) or not 1 <= len(request["handle"]) <= 2048:
            raise ValueError("observation exact handle is required")
        request.setdefault("offset_bytes", 0)
        request.setdefault("max_bytes", 65536)
        if type(request["offset_bytes"]) is not int or request["offset_bytes"] < 0:
            raise ValueError("observation byte offset must be nonnegative")
        if type(request["max_bytes"]) is not int or not 1 <= request["max_bytes"] <= 65536:
            raise ValueError("observation byte limit must be 1..65536")
        if ("source_evidence" in request) != ("source_ordinal" in request):
            raise ValueError("source traversal requires an exact Evidence and ordinal")
        if "source_evidence" in request and (
            not isinstance(request["source_evidence"], str)
            or not 1 <= len(request["source_evidence"]) <= 2048
            or type(request["source_ordinal"]) is not int or request["source_ordinal"] < 0
        ):
            raise ValueError("source traversal selection is malformed")
    if set(request) - allowed:
        raise ValueError("observation request has unsupported fields")
    binding = request.get("source_binding")
    if ("source_binding" in request and binding is None) or (operation == "exact_record" and binding is None):
        raise ValueError("observation exact reads require a non-null source binding")
    if binding is not None:
        if not isinstance(binding, Mapping) or set(binding) != _BINDING_KEYS:
            raise ValueError("observation source binding has the wrong shape")
        if type(binding["project_commit"]) is not int or binding["project_commit"] < 0:
            raise ValueError("observation source cut is invalid")
        for field in ("root_digest", "canonical_authority_digest"):
            if not isinstance(binding[field], str) or re.fullmatch(r"[0-9a-f]{64}", binding[field]) is None:
                raise ValueError("observation source digest is invalid")
        if binding["transition_head_digest"] is not None and (
            not isinstance(binding["transition_head_digest"], str)
            or re.fullmatch(r"[0-9a-f]{64}", binding["transition_head_digest"]) is None
        ):
            raise ValueError("observation transition digest is invalid")
        if any(not isinstance(binding[field], str) or not binding[field] for field in ("project_id", "mission_id", "root_identity")):
            raise ValueError("observation source identity is invalid")
    if request.get("cursor") is not None and binding is None:
        raise ValueError("observation continuation requires its source binding")
    return request


def _binding(service: Any, cut: Mapping[str, Any]) -> dict[str, Any]:
    return {"project_id": service._store.project_id, "mission_id": service._mission_id,
            **{key: cut[key] for key in _BINDING_KEYS - {"project_id", "mission_id"}}}


def _result(request: Mapping[str, Any], binding: Mapping[str, Any], *, items: list[Any] | None = None,
            cursor: str | None = None, state: str | None = None, reason: str | None = None,
            coverage: Mapping[str, Any] | None = None) -> dict[str, Any]:
    rows = [] if items is None else items
    return {"schema_version": SCHEMA, "operation": request["operation"], "source_binding": deep_thaw(binding),
            "state": state or ("present" if rows else "empty"), "reason": reason,
            "completeness": "page" if cursor is not None else "exhausted", "items": rows,
            "next_cursor": cursor, "coverage": None if coverage is None else deep_thaw(coverage)}


def _owner(service: Any, handle: str, cut: int) -> Mapping[str, Any]:
    kind, identity, revision = service._parse_retrieval_id(handle)
    if kind not in OWNER_KINDS or revision is None:
        raise _Denied("exact_allowed_owner_revision_required")
    selected = service._store.read_indexed_observation_owner(mission_id=service._mission_id,
        kind=kind, identity=identity, revision=revision, source_project_commit=cut)
    if selected is None:
        raise _Unavailable("owner_not_retained_at_cut")
    owner = selected["owner"]
    if owner["mission_id"] != service._mission_id:
        raise _Denied("wrong_mission")
    if kind == "evidence" and owner["document"].get("subtype") != "evidence_meaning":
        raise _Denied("reserved_evidence")
    if kind == "candidate" and (candidate_a1_requirement(owner["document"]) is not None
                                or owner["document"].get("full_rh_case")):
        raise _Denied("reserved_candidate")
    return owner


def _selected(service: Any, cut: int) -> tuple[Mapping[str, Any], Mapping[str, Any]]:
    mission = service._store.read_indexed_observation_owner(mission_id=service._mission_id,
        kind="mission", identity=service._mission_id, source_project_commit=cut)
    if mission is None or mission["owner"]["mission_id"] != service._mission_id:
        raise _Unavailable("mission_not_retained_at_cut")
    mission_owner = mission["owner"]
    strategy_ids = mission_owner["document"].get("strategy_ids")
    if not isinstance(strategy_ids, (list, tuple)) or len(strategy_ids) != 1:
        raise WorkspaceIntegrityError("observation Mission does not select one Strategy")
    strategy = service._store.read_indexed_observation_owner(mission_id=service._mission_id,
        kind="strategy", identity=str(strategy_ids[0]), source_project_commit=cut)
    if strategy is None or strategy["owner"]["mission_id"] != service._mission_id:
        raise WorkspaceIntegrityError("observation selected Strategy is absent from its cut")
    return mission_owner, strategy["owner"]


def _semantic(service: Any, owner: Mapping[str, Any]) -> dict[str, Any]:
    reference = deep_thaw(owner["reference"])
    document = semantic_owner_document(reference["kind"], owner["document"])
    if reference["kind"] == "strategy":
        from .mission_frontier import PersistedStrategyRevision, StrategyRevision
        from .mission_interface import _formal_request_projection
        provenance = owner["revision_provenance"]
        strategy = PersistedStrategyRevision(StrategyRevision(owner["document"], reference["payload_sha256"]),
            reference["revision"], provenance["predecessor_revision"], provenance["created_actor"], provenance["created_at"])
        document["formal_requests"] = _formal_request_projection(strategy)
    return {"reference": reference, "handle": _ref_handle(reference), "semantic": document}


def _checkpoint(service: Any, identity: str) -> Mapping[str, Any]:
    checkpoint = service._store.read_continuation_checkpoint(checkpoint_id=identity)
    if checkpoint is None:
        raise _Unavailable("checkpoint_not_retained")
    if checkpoint["mission_id"] != service._mission_id:
        raise _Denied("wrong_mission")
    return checkpoint


def _anchor(checkpoint: Mapping[str, Any]) -> dict[str, Any]:
    document = checkpoint.get("document", checkpoint)
    return {"handle": f"checkpoint:{checkpoint['checkpoint_id']}",
            "checkpoint_id": checkpoint["checkpoint_id"], "payload_sha256": checkpoint["payload_digest"],
            "project_commit": int(checkpoint["project_commit_no"]),
            "mission_reference": deep_thaw(document["mission_root"]),
            "strategy_reference": deep_thaw(document["strategy_root"]),
            "predecessor_checkpoint": deep_thaw(document["predecessor_checkpoint"])}


def _current(service: Any, cut: int) -> dict[str, Any]:
    mission, strategy = _selected(service, cut)
    checkpoint = service._store.read_current_mission_checkpoint_summary(service._mission_id)
    if checkpoint is not None and int(checkpoint["project_commit_no"]) > cut:
        # No unbounded predecessor walk disguised as a current-state read.
        raise _Unavailable("current_checkpoint_has_advanced_refresh_current_decision")
    return {"kind": "current_decision", "mission": _semantic(service, mission),
            "strategy": _semantic(service, strategy), "checkpoint": None if checkpoint is None else _anchor(checkpoint)}


def _history(service: Any, request: Mapping[str, Any], cut: int, position: Any) -> tuple[list[Any], Any]:
    size = request["page_size"]
    if request["collection"] == "strategies":
        batch = service._store.read_owner_content_inventory_page(
            mission_id=service._mission_id, projection_variant="root", kinds=("strategy",),
            source_project_commit=cut, revision_scope="retained_history", after=position, page_size=size)
        rows = []
        for descriptor in batch["items"]:
            owner = _owner(service, str(descriptor["handle"]), cut)
            if deep_thaw(owner["reference"]) != deep_thaw(descriptor["reference"]):
                raise WorkspaceIntegrityError("observation Strategy differs from indexed exact source")
            reference = owner["reference"]
            predecessor = owner["revision_provenance"]["predecessor_revision"]
            before = None if predecessor is None else deep_thaw(_owner(
                service, f"strategy:{reference['identity']}@{predecessor}", cut)["reference"])
            current = service._store.read_indexed_observation_owner(mission_id=service._mission_id,
                kind="strategy", identity=reference["identity"], source_project_commit=cut)
            rows.append({"kind": "strategy_transition", "project_commit": int(descriptor["source_origin_project_commit"]),
                         "temporal_status": "current_at_cut" if current is not None and current["revision"] == reference["revision"] else "historical_at_cut",
                         "before": before, "after": deep_thaw(reference), "handle": _ref_handle(reference)})
        return rows, deep_thaw(batch["next_after"])
    if position is None:
        summary = service._store.read_current_mission_checkpoint_summary(service._mission_id)
        position = {"checkpoint_id": None if summary is None else str(summary["checkpoint_id"]), "latest_at_cut": None}
    checkpoint_id = position["checkpoint_id"]
    latest_at_cut = position["latest_at_cut"]
    rows = []
    # One predecessor hop per examined row. Later checkpoints can yield an empty
    # nonterminal page; never refill by walking an unbounded newer interval.
    for _ in range(size):
        if checkpoint_id is None:
            break
        checkpoint = _checkpoint(service, checkpoint_id)
        predecessor = checkpoint["document"]["predecessor_checkpoint"]
        checkpoint_id = None if predecessor is None else str(predecessor["checkpoint_id"])
        if int(checkpoint["project_commit_no"]) > cut:
            continue
        if latest_at_cut is None:
            latest_at_cut = str(checkpoint["checkpoint_id"])
        before = None
        if predecessor is not None:
            previous = _checkpoint(service, str(predecessor["checkpoint_id"]))
            if previous["payload_digest"] != predecessor["payload_sha256"] or int(previous["project_commit_no"]) >= int(checkpoint["project_commit_no"]):
                raise WorkspaceIntegrityError("observation checkpoint predecessor binding changed")
            before = _anchor(previous)
        rows.append({"kind": "checkpoint_transition", "project_commit": int(checkpoint["project_commit_no"]),
                     "temporal_status": "current_at_cut" if checkpoint["checkpoint_id"] == latest_at_cut else "historical_at_cut",
                     "before": before, "after": _anchor(checkpoint)})
    return rows, None if checkpoint_id is None else {"checkpoint_id": checkpoint_id, "latest_at_cut": latest_at_cut}


def _exact(service: Any, request: Mapping[str, Any], cut: int) -> dict[str, Any]:
    handle = str(request["handle"])
    relation = None
    reference = None
    content_digest = None
    page = None
    total_bytes = None
    if "source_evidence" in request and not handle.startswith("capture-artifact:"):
        raise _Denied("source_relation_requires_capture_artifact")
    if handle.startswith("checkpoint:"):
        checkpoint = _checkpoint(service, handle[len("checkpoint:"):])
        if int(checkpoint["project_commit_no"]) > cut:
            raise _Unavailable("checkpoint_not_retained_at_cut")
        reference = _anchor(checkpoint)
        raw = canonical_json_bytes(reference)
        trust = "custody_descriptor"
        media_type = "application/json"
    elif handle.startswith("capture-artifact:"):
        evidence_handle = request.get("source_evidence")
        if not isinstance(evidence_handle, str) or not evidence_handle.startswith("evidence:"):
            raise _Denied("recorded_evidence_source_required")
        evidence = _owner(service, evidence_handle, cut)
        evidence_ref = evidence["reference"]
        selected = service._store.read_evidence_source_artifact_page(
            mission_id=service._mission_id, evidence_id=evidence_ref["identity"],
            revision=evidence_ref["revision"], expected_payload_digest=evidence_ref["payload_sha256"],
            source_project_commit=cut, source_ordinal=request["source_ordinal"],
            artifact_handle=handle, offset_bytes=request["offset_bytes"], max_bytes=request["max_bytes"],
        )
        if selected is None:
            raise _Denied("artifact_not_in_selected_evidence_relation")
        descriptor, source = selected["descriptor"], selected["source"]
        page, total_bytes = selected["content"], selected["total_bytes"]
        # The Store scanned the full selected Blob once and checked every other
        # related Blob. Only the verified page is retained by this projection.
        content_digest = descriptor["blob_sha256"]
        reference = {"capture_id": descriptor["capture_id"], "artifact_ordinal": descriptor["artifact_ordinal"], "blob_sha256": descriptor["blob_sha256"]}
        relation = {"evidence_reference": deep_thaw(evidence_ref), "source_ordinal": source["source_ordinal"], "exact_scope": deep_thaw(source["exact_scope"])}
        trust = "untrusted_raw_material"
        media_type = descriptor["media_type"]
    else:
        owner = _owner(service, handle, cut)
        if owner["reference"]["kind"] == "evidence":
            raw = canonical_json_bytes(_evidence_document(service, owner, cut))
        else:
            raw = canonical_json_bytes(_semantic(service, owner)["semantic"])
        reference = deep_thaw(owner["reference"])
        trust, media_type = "semantic_owner_projection", "application/json"
    offset = request["offset_bytes"]
    if page is None:
        total_bytes = len(raw)
        if offset > total_bytes:
            raise ValueError("observation byte offset exceeds exact extent")
        page = raw[offset:offset + request["max_bytes"]]
    next_offset = offset + len(page)
    if content_digest is None:
        content_digest = hashlib.sha256(raw).hexdigest()
    return {"kind": "exact_record", "handle": handle, "reference": reference, "source_relation": relation,
            "trust_class": trust, "media_type": media_type, "encoding": "base64", "sha256": content_digest,
            "total_bytes": total_bytes, "offset_bytes": offset, "returned_bytes": len(page),
            "page_sha256": hashlib.sha256(page).hexdigest(), "content_base64": base64.b64encode(page).decode("ascii"),
            "next_offset_bytes": next_offset if next_offset < total_bytes else None}


def _evidence_document(service: Any, owner: Mapping[str, Any], cut: int) -> dict[str, Any]:
    reference = owner["reference"]
    evidence = service._store.read_evidence_meaning_revision(reference["identity"], reference["revision"],
        source_project_commit=cut)
    if evidence["payload_digest"] != reference["payload_sha256"]:
        raise WorkspaceIntegrityError("observation Evidence changed its authenticated payload")
    document = semantic_owner_document("evidence", evidence["record"])
    document["source_navigation"] = evidence_source_navigation(evidence["sources"])
    return document


def _catalog(service: Any, request: Mapping[str, Any], cut: int, observed: int, position: Any) -> tuple[list[Any], Any, dict[str, Any]]:
    collection, size = request["collection"], request["page_size"]
    coverage = {"collection": collection, "scope": "selected_mission_science", "ordering": "authored_order",
                "source_inventory_count": None, "record_bodies": "not_exposed_by_catalog"}
    def row(field: str, index: int, value: Mapping[str, Any], relationship: str | None = None) -> dict[str, Any]:
        return {"source_field": field, "source_index": index, "relationship_class": relationship, "value": deep_thaw(value)}
    if collection == "latest_outputs":
        coverage.update(scope="selected_epoch_captured_outputs", ordering="ascending_capture_identity")
        if position is None:
            if cut != observed:
                raise _Unavailable("output_catalog_first_page_requires_current_cut")
            epoch = service._store.read_latest_executive_epoch_events(service._mission_id)
            epoch_id = None if not epoch else str(epoch[0]["executive_epoch_id"])
            position = {"after": None, "epoch_id": epoch_id}
        batch = service._store.read_observation_capture_page(mission_id=service._mission_id, source_project_commit=cut,
                    origin_epoch_id=position["epoch_id"], capture_kind="output", after=position["after"], page_size=size)
        rows = []
        for index, descriptor in enumerate(batch["records"]):
            capture = descriptor["descriptor"]
            rows.append(row("raw_captures", index, {"handle": capture["id"],
                "capture_kind": "output", "executive_epoch_id": capture["origin_epoch_id"],
                "project_commit": descriptor["origin_project_commit"], "artifact_count": len(capture["artifacts"])}))
        return rows, None if batch["next_after"] is None else {**position, "after": deep_thaw(batch["next_after"])}, coverage
    if collection == "open_candidates":
        coverage.update(scope="live_open_a1_references", ordering="owner_order")
        if cut != observed:
            raise _Expired("live_candidate_catalog_cut_changed")
        values = [row("open_candidate_a1_refs", index, item) for index, item in enumerate(service._current_open_candidate_a1())]
    else:
        mission, strategy = _selected(service, cut)
        if collection == "frontier":
            values = [row("mission", 0, {"handle": _ref_handle(mission["reference"]), "reference": mission["reference"],
                                         "summary": semantic_owner_document("mission", mission["document"])}),
                      row("current_strategy", 0, {"handle": _ref_handle(strategy["reference"]), "reference": strategy["reference"],
                         "mission_continuation": strategy["document"].get("mission_continuation"),
                         "selected_bets": strategy["document"].get("selected_bets", [])})]
        else:
            values = []
            if collection == "owner_records":
                values.extend(row(field, 0, {"handle": _ref_handle(owner["reference"]), "reference": owner["reference"], "connections": []})
                              for field, owner in (("mission", mission), ("current_strategy", strategy)))
            scientific_context_id = mission["document"].get("scientific_context_id")
            if isinstance(scientific_context_id, str):
                context = service._store.read_indexed_observation_owner(mission_id=service._mission_id,
                    kind="context", identity=scientific_context_id, source_project_commit=cut)
                if context is None or context["owner"]["mission_id"] != service._mission_id:
                    raise WorkspaceIntegrityError("observation Mission scientific Context is absent from its cut")
                reference = context["owner"]["reference"]
                connection = {"json_pointer": "/scientific_context_id", "role": "scientific_context_binding"}
                if collection == "owner_records":
                    values.append(row("scientific_context", 0, {"handle": _ref_handle(reference), "reference": reference, "connections": [connection]}))
                else:
                    values.append(row("scientific_context", 0, {"source_handle": _ref_handle(mission["reference"]),
                        "target_handle": _ref_handle(reference), "source_path": connection["json_pointer"]}, connection["role"]))
            if collection == "relationship_hooks":
                coverage["scope"] = "mission_binding_and_strategy_references"
            for index, (key, connections) in enumerate(strategy_connections(strategy["document"]).items()):
                kind, identity, revision, digest = key
                reference = {"kind": kind, "identity": identity, "revision": revision, "payload_sha256": digest}
                if collection == "owner_records":
                    values.append(row("strategy_ground", index, {"handle": _ref_handle(reference), "reference": reference,
                                                                  "connections": connections}))
                else:
                    for connection in connections:
                        values.append(row("strategy_connections", index,
                            {"source_handle": _ref_handle(strategy["reference"]), "target_handle": _ref_handle(reference),
                             "source_path": connection["json_pointer"]}, connection["role"]))
    offset = 0 if position is None else position
    if type(offset) is not int or offset < 0 or offset > len(values):
        raise ValueError("observation catalog continuation is invalid")
    coverage["source_inventory_count"] = len(values)
    page = values[offset:offset + size]
    return page, offset + size if offset + size < len(values) else None, coverage


def observe_mission(service: Any, request: Mapping[str, Any], *, cursor_mac_key: str) -> dict[str, Any]:
    """Read the closed operator surface after fixed-transport authorization."""
    query = validate_observation_request(request)
    if not isinstance(cursor_mac_key, str) or re.fullmatch(r"[0-9a-f]{64}", cursor_mac_key) is None:
        raise ValueError("observation requires private transport signing context")
    with service._store.direct_recovery_read_scope():
        current = service._store.read_observation_cut()
        requested_binding = query.get("source_binding")
        cut = current
        if requested_binding is not None:
            if requested_binding["project_id"] != service._store.project_id or requested_binding["mission_id"] != service._mission_id or requested_binding["root_identity"] != current["root_identity"] or requested_binding["project_commit"] > current["project_commit"]:
                return _result(query, requested_binding, state="expired", reason="source_incarnation_or_cut_changed")
            cut = service._store.read_observation_cut(cut_project_commit=requested_binding["project_commit"])
            if _binding(service, cut) != requested_binding:
                return _result(query, requested_binding, state="expired", reason="source_binding_changed")
        binding = _binding(service, cut)
        if cut["route"] != "schema12_owner_content":
            return _result(query, binding, state="unavailable", reason="owner_content_index_not_introduced_at_cut")
        scope = {"schema_version": SCHEMA, "source_binding": binding,
                 "operation": query["operation"], "collection": query.get("collection"), "page_size": query.get("page_size")}
        position = None
        if query.get("cursor") is not None:
            try:
                position = json.loads(service._store.read_historical_read_cursor(
                    query["cursor"], grant_id="operator-observation:" + cursor_mac_key, scope=scope))
            except (TypeError, ValueError):
                return _result(query, binding, state="expired", reason="cursor_binding_changed")
        try:
            continuation = None
            coverage = None
            selected_cut = int(cut["project_commit"])
            if query["operation"] == "current_decision":
                items = [_current(service, selected_cut)]
            elif query["operation"] == "exact_record":
                items = [_exact(service, query, selected_cut)]
            elif query["operation"] == "decision_history":
                items, continuation = _history(service, query, selected_cut, position)
                coverage = {"collection": query["collection"], "scope": "retained_owner_transitions", "ordering": "owner_identity_revision" if query["collection"] == "strategies" else "reverse_checkpoint_chain",
                            "source_inventory_count": None, "record_bodies": "not_exposed_by_catalog"}
            else:
                items, continuation, coverage = _catalog(service, query, selected_cut, int(current["project_commit"]), position)
            cursor = None if continuation is None else service._store.issue_historical_read_cursor(
                grant_id="operator-observation:" + cursor_mac_key, scope=scope,
                position=canonical_json_bytes(continuation).decode("utf-8"))
            result = _result(query, binding, items=items, cursor=cursor, coverage=coverage)
            if len(canonical_json_bytes(result)) > 1024 * 1024:
                return _result(query, binding, state="unavailable", reason="observation_response_extent_exceeded")
            return result
        except _Denied as exc:
            return _result(query, binding, state="denied", reason=str(exc))
        except _Expired as exc:
            return _result(query, binding, state="expired", reason=str(exc))
        except _Unavailable as exc:
            return _result(query, binding, state="unavailable", reason=str(exc))
        except RawCaptureArtifactUnavailableError as exc:
            return _result(query, binding, state="unavailable", reason=str(exc.disposition))
        except (StaleCommandError, LookupError):
            return _result(query, binding, state="unavailable", reason="selected_record_not_retained")
