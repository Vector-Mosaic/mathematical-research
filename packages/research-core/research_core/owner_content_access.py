"""Store-private adapter for authenticated, derived owner-content access.

Only the Store calls the mutation/bootstrap functions, in its existing SQLite
transaction. This module owns no journal, commits, query authority or migration
lifecycle. The AVL owns authenticated traversal; the outward projection owner
owns searchable strings. Source origins and projection occurrences are distinct.
"""
from __future__ import annotations

from collections.abc import Callable, Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from heapq import heappop, heappush
from contextlib import ExitStack, closing
from typing import Any
import hashlib
import json
import sqlite3

from .json_support import canonical_json_bytes
from .research_model import deep_freeze
from .owner_content_projection import (
    PROFILE_DIGEST, PROJECTION_FIELDS,
    current_checkpoint_fields, project_delegated_artifact_fields,
    project_delegated_capture_fields, project_delegated_owner_fields,
    project_root_capture_fields, project_root_owner_fields,
)


def _integrity(message: str) -> None:
    from .workspace_store import WorkspaceIntegrityError
    raise WorkspaceIntegrityError(message)


class _NotSearchableSource(ValueError):
    """Exact retained owner is outside the existing public search projection."""


def _searchable_owner(kind: str, document: Mapping[str, Any]) -> bool:
    # Candidate-A1 preservation Evidence is recovery-internal, not an ordinary
    # Mission EvidenceMeaningRecord. Existing root and child search exclude it;
    # its custody/proof-attention/exact recovery authorities remain unchanged.
    return not (kind == "evidence" and document.get("subtype") == "purported_complete_route"
                and _mission_id(document, kind) is None)


def _variant(value: str) -> str:
    # The version is part of the descriptor's profile, not an independent lane.
    normalized = {"root_v1": "root", "delegated_v1": "delegated"}.get(value, value)
    if normalized not in PROJECTION_FIELDS:
        raise ValueError("unsupported owner-content projection variant")
    return normalized


@dataclass(frozen=True)
class SourceProjection:
    """One source authenticated by the caller or issued by its current write.

    `origin_commit` identifies this projection occurrence. Immutable owners use
    their source origin; a cut-dependent Capture descriptor uses its change cut,
    retaining the immutable custody origin in `source_origin_commit`.
    """

    project_id: str
    mission_id: str
    kind: str
    identity: str
    revision: int
    payload_digest: str
    origin_commit: int
    document: Mapping[str, Any]
    fields: Mapping[str, Mapping[str, str]]
    source_origin_commit: int | None = None
    selected_at_commit: int | None = None
    retired_at_commit: int | None = None
    inventory_metadata: Mapping[str, Any] = field(default_factory=dict)

    @property
    def handle(self) -> str:
        suffix = "" if self.kind in {"capture", "capture-artifact"} else f"@{self.revision}"
        return f"{self.kind}:{self.identity}{suffix}"

    @property
    def reference(self) -> dict[str, Any]:
        return {"kind": self.kind, "identity": self.identity,
                "revision": 0 if self.kind in {"capture", "capture-artifact"} else self.revision,
                "payload_sha256": self.payload_digest}

    def __post_init__(self) -> None:
        if not all(isinstance(v, str) and v for v in (self.project_id, self.mission_id, self.kind, self.identity)):
            raise ValueError("owner-content source requires exact scope and identity")
        if type(self.revision) is not int or self.revision < 0 or type(self.origin_commit) is not int or self.origin_commit < 0:
            raise ValueError("owner-content revision/origin must be nonnegative integers")
        if len(self.payload_digest) != 64 or any(c not in "0123456789abcdef" for c in self.payload_digest):
            raise ValueError("owner-content source digest must be SHA-256")
        if self.source_origin_commit is not None and (
            type(self.source_origin_commit) is not int or not 0 <= self.source_origin_commit <= self.origin_commit
        ):
            raise ValueError("source origin must precede projection occurrence")
        for value in (self.selected_at_commit, self.retired_at_commit):
            if value is not None and (type(value) is not int or value < self.origin_commit):
                raise ValueError("selection/retirement must follow exact source origin")
        if not self.fields:
            raise ValueError("owner-content source requires its supported projections")
        for variant, values in self.fields.items():
            if variant not in PROJECTION_FIELDS or self.kind not in PROJECTION_FIELDS[variant]:
                raise ValueError("owner-content source has an unsupported projection")
            if set(values) != set(PROJECTION_FIELDS[variant][self.kind]) or any(not isinstance(v, str) for v in values.values()):
                raise ValueError("owner-content source omitted a declared search field")
        object.__setattr__(self, "document", deep_freeze(self.document))
        object.__setattr__(self, "fields", deep_freeze(self.fields))
        if set(self.inventory_metadata) - {"capture_descriptor"}:
            raise ValueError("owner-content inventory metadata is outside its closed projection")
        if self.inventory_metadata and (self.kind != "capture" or set(self.fields) != {"root"}):
            raise ValueError("only root Capture occurrences carry derived descriptor metadata")
        object.__setattr__(self, "inventory_metadata", deep_freeze(self.inventory_metadata))


def _index(connection: sqlite3.Connection, descriptor: Mapping[str, Any] | None = None):
    from .owner_content_index import OwnerContentIndex
    from .workspace_store import WorkspaceIntegrityError
    return OwnerContentIndex(connection, PROFILE_DIGEST, descriptor,
                             integrity_error=WorkspaceIntegrityError)


def _source_common(source: SourceProjection, revision_scope: str) -> dict[str, Any]:
    if revision_scope not in {"retained_history", "current_at_cut"}:
        raise ValueError("unsupported owner-content revision scope")
    return dict(project_id=source.project_id, mission_id=source.mission_id,
                  kind=source.kind, owner_id=source.identity, revision=source.revision,
                  reference=source.handle, payload_digest=source.payload_digest,
                  origin_commit=source.origin_commit,
                  source_origin_commit=source.source_origin_commit,
                  metadata={"reference": source.reference,
                            "selected_at_commit": source.selected_at_commit,
                            "retired_at_commit": source.retired_at_commit})


def _source_descriptor_entries(source: SourceProjection, revision_scope: str,
                               common: Mapping[str, Any]) -> Iterator[Any]:
    from .owner_content_index import (
        Posting, _validate_posting_value, capture_epoch_kind_directory_key,
        entries_for_inventory, entries_for_presence,
    )
    for variant in source.fields:
        inventory_common = {**common, "metadata": {**common["metadata"], **dict(source.inventory_metadata)}}
        yield from entries_for_inventory(**inventory_common, revision_scope=revision_scope, variant=variant)
        if revision_scope == "current_at_cut" and variant == "root" and source.kind == "capture":
            # This is a selector of the same authenticated occurrence, not a
            # second descriptor or an index maintained by the observer.
            descriptor = source.inventory_metadata.get("capture_descriptor")
            if (not isinstance(descriptor, Mapping)
                    or "executive_epoch_id" not in source.document
                    or "capture_kind" not in source.document):
                raise ValueError("root Capture membership requires its exact custody selectors")
            epoch_id, capture_kind = source.document["executive_epoch_id"], source.document["capture_kind"]
            directory = capture_epoch_kind_directory_key(
                source.project_id, source.mission_id, epoch_id, capture_kind,
            )
            if (descriptor.get("id") != source.handle
                    or "origin_epoch_id" not in descriptor
                    or descriptor["origin_epoch_id"] != epoch_id
                    or descriptor.get("capture_kind") != capture_kind):
                raise ValueError("root Capture membership differs from its committed descriptor")
            posting = Posting((source.kind, source.identity, source.revision),
                source.handle, source.payload_digest, source.origin_commit,
                source_origin_commit=source.source_origin_commit, metadata=common["metadata"])
            _validate_posting_value(posting.key, posting.to_mapping(), directory)
            yield directory, posting
    if revision_scope == "current_at_cut" and current_checkpoint_fields(source.kind, source.document):
        yield from entries_for_presence(**{k: v for k, v in common.items() if k != "revision"}, document=source.document)


def source_descriptor_entries(source: SourceProjection, revision_scope: str) -> Iterator[Any]:
    """Validated nonlexical source relation, without expanding lexical postings.

    Inventory retains all Capture qualifications. Presence remains once per
    source, not once per projection variant. Source authentication belongs to
    the caller, exactly as for source_entries.
    """
    yield from _source_descriptor_entries(source, revision_scope, _source_common(source, revision_scope))


def source_entries(source: SourceProjection, revision_scope: str) -> Iterator[Any]:
    from .owner_content_index import entries_for_fields
    common = _source_common(source, revision_scope)
    for directory, posting in _source_descriptor_entries(source, revision_scope, common):
        yield directory, posting
        if directory[0] == "owner_inventory":
            variant = directory[4]
            yield from entries_for_fields(**common, revision_scope=revision_scope,
                                          variant=variant, fields=source.fields[variant])


@dataclass(frozen=True)
class OwnerContentDelta:
    """Exact old current memberships and newly issued source projections."""
    removed_current: tuple[SourceProjection, ...] = ()
    added_retained: tuple[SourceProjection, ...] = ()
    added_current: tuple[SourceProjection, ...] = ()
    removed_retained: tuple[SourceProjection, ...] = ()
    pending_capture_projection: bool = False


def apply_owner_content_delta(
    connection: sqlite3.Connection, *, descriptor: Mapping[str, Any],
    delta: OwnerContentDelta,
) -> Mapping[str, Any]:
    if not connection.in_transaction:
        _integrity("owner-content maintenance requires the Store writer transaction")
    if not isinstance(descriptor, Mapping):
        _integrity("owner-content maintenance has no authenticated prior descriptor")
    if delta.pending_capture_projection:
        _integrity("owner-content transaction has not completed its root Capture projection")
    index = _index(connection, descriptor)
    return index.apply_delta(delta)


def bootstrap_owner_content_index(
    connection: sqlite3.Connection, *, project_id: str, source_project_commit: int,
    retained_sources: Iterable[SourceProjection], current_sources: Iterable[SourceProjection],
) -> Mapping[str, Any]:
    """Explicit migration-only full derivation; ordinary queries never call it."""
    if not connection.in_transaction:
        _integrity("owner-content bootstrap requires the migration transaction")
    index = _index(connection)

    def inventory():
        for scope, sources in (("retained_history", retained_sources), ("current_at_cut", current_sources)):
            for source in sources:
                if source.project_id != project_id or source.origin_commit > source_project_commit:
                    _integrity("owner-content bootstrap source escaped its authenticated cut")
                yield scope, source

    return index.bootstrap(inventory())


def verify_owner_content_relation(
    connection: sqlite3.Connection, *, descriptor: Mapping[str, Any],
    retained_sources: Iterable[SourceProjection], current_sources: Iterable[SourceProjection],
) -> None:
    """Explicit complete audit independently rederives owner/index membership.

    This intentionally has full-corpus cost, unlike ordinary query and mutation.
    The caller authenticates eligible source inventory using existing full audit.
    """
    if not isinstance(descriptor, Mapping):
        _integrity("owner-content audit has no authenticated descriptor")
    actual = _index(connection, descriptor)
    # Expected facts come only from independently authenticated source owners.
    # The single audit checks complete graph/inverses AND source equality;
    # do not expand the same lexical corpus into Postings first or traverse it twice.
    actual.audit_sources((scope, source)
        for scope, sources in (("retained_history", retained_sources), ("current_at_cut", current_sources))
        for source in sources)


def query_index_candidates(
    connection: sqlite3.Connection, *, project_id: str, mission_id: str,
    descriptor: Mapping[str, Any], projection_variant: str, kinds: Sequence[str],
    fields: Sequence[str], query: str, source_project_commit: int,
    revision_scope: str, after: Sequence[Any] | None = None, page_size: int = 25,
    authenticated_source: Callable[[Mapping[str, Any]], SourceProjection] | None = None,
    head_at_cut: Callable[[Mapping[str, Any], int], bool] | None = None,
    prebootstrap_current: bool = False,
    root_capture_projection: Callable[[Mapping[str, Any], int, int], Mapping[str, Any]] | None = None,
) -> Mapping[str, Any]:
    """Merge authenticated positional streams without materializing all matches.

    The descriptor authenticates completeness of candidates, never the source's
    mathematical validity. Re-read/authenticate only matched sources and verify
    actual substring before returning. A bootstrap historical-current query also
    requires exact head-at-cut selection; current postings never substitute for
    that earlier state. Every cursor resumes from the last *returned* owner key.
    """
    variant = _variant(projection_variant)
    if not isinstance(descriptor, Mapping):
        _integrity("owner-content indexed query is unavailable without its authenticated descriptor")
    selected_kinds = tuple(sorted(set(kinds)))
    selected_fields = tuple(dict.fromkeys(fields))
    inventory = query == "" and not selected_fields
    if not selected_kinds or any(k not in PROJECTION_FIELDS[variant] for k in selected_kinds):
        raise ValueError("search must select supported, nonempty source kinds")
    if not inventory and (not selected_fields or any(f not in PROJECTION_FIELDS[variant][k] for k in selected_kinds for f in selected_fields)):
        raise ValueError("search must select supported, nonempty source fields")
    if not isinstance(query, str) or (not query and not inventory) or type(page_size) is not int or not 1 <= page_size <= 1000:
        raise ValueError("search requires a nonempty query and page size 1..1000")
    if type(source_project_commit) is not int or source_project_commit < 0:
        raise ValueError("search source cut must be nonnegative")
    if revision_scope not in {"retained_history", "current_at_cut"}:
        raise ValueError("search revision scope is unsupported")
    if prebootstrap_current and (revision_scope != "current_at_cut" or head_at_cut is None):
        raise ValueError("pre-bootstrap current search requires exact historical head selection")
    after_key = None if after is None else tuple(after)
    if after_key is not None and (len(after_key) != 3 or not all(isinstance(x, str) for x in after_key[:2]) or type(after_key[2]) is not int):
        raise ValueError("search continuation must be one exact ordered posting key")
    index = _index(connection, descriptor)
    with index.operation(), ExitStack() as operations:
        scope = "retained_history" if prebootstrap_current else revision_scope
        heap: list[Any] = []
        streams: list[Iterator[Any]] = []
        for kind in selected_kinds:
            for field in ("",) if inventory else selected_fields:
                from .owner_content_index import inventory_directory_key
                stream = iter(index.postings(inventory_directory_key(
                    project_id, mission_id, scope, variant, kind), after=after_key
                ) if inventory else index.search_field(
                    directory_prefix=("lexical", project_id, mission_id, scope, variant, kind, field),
                    query=query, after=after_key, origin_commit_at_most=source_project_commit,
                ))
                ordinal = len(streams)
                streams.append(stream)
                operations.callback(stream.close)
                posting = next(stream, None)
                if posting is not None:
                    heappush(heap, (tuple(posting.key), ordinal, field, posting))
        result: list[Mapping[str, Any]] = []
        returned_key: tuple[Any, ...] | None = None
        while heap:
            key = heap[0][0]
            matches: list[str] = []
            reference_posting = None
            while heap and heap[0][0] == key:
                _, ordinal, field, posting = heappop(heap)
                if reference_posting is not None and (
                    posting.reference != reference_posting.reference or
                    posting.payload_digest != reference_posting.payload_digest or
                    posting.origin_commit != reference_posting.origin_commit or
                    posting.source_origin_commit != reference_posting.source_origin_commit or
                    dict(posting.metadata) != dict(reference_posting.metadata)
                ):
                    _integrity("owner-content field streams disagree about their exact source")
                reference_posting = posting
                matches.append(field)
                next_posting = next(streams[ordinal], None)
                if next_posting is not None:
                    heappush(heap, (tuple(next_posting.key), ordinal, field, next_posting))
            assert reference_posting is not None
            posting = reference_posting
            if posting.origin_commit > source_project_commit:
                continue
            reference = dict(posting.metadata.get("reference", {}))
            if (set(reference) != {"kind", "identity", "revision", "payload_sha256"}
                or reference["kind"] != key[0] or reference["identity"] != key[1]
                or reference["revision"] != (0 if key[0] in {"capture", "capture-artifact"} else key[2])
                or reference["payload_sha256"] != posting.payload_digest):
                _integrity("owner-content posting metadata differs from its exact source key")
            expected_handle = f"{key[0]}:{key[1]}" + ("" if key[0] in {"capture", "capture-artifact"} else f"@{key[2]}")
            if posting.reference != expected_handle:
                _integrity("owner-content posting handle differs from its exact source key")
            if prebootstrap_current:
                if variant == "root" and reference["kind"] == "capture":
                    # A Capture has one immutable custody identity but several
                    # historical root-metadata occurrences. Select its last exact
                    # occurrence at C, not every old occurrence containing a term.
                    with closing(index.postings(inventory_directory_key(
                        project_id, mission_id, "retained_history", "root", "capture"
                    ), after=key)) as following:
                        successor = next(following, None)
                    if successor is not None and successor.key[:2] == key[:2] and successor.origin_commit <= source_project_commit:
                        continue
                elif not head_at_cut(reference, source_project_commit):
                    continue
            item = {"reference": reference, "handle": posting.reference,
                    "origin_project_commit": posting.origin_commit,
                    "source_origin_project_commit": posting.source_origin_commit if posting.source_origin_commit is not None else posting.origin_commit,
                    "projection_commit": posting.origin_commit,
                    "matched_fields": [field for field in selected_fields if field in matches]}
            if inventory:
                # The serving descriptor authenticates this derived identity and
                # origin. Bodies remain behind separately authorized exact reads.
                if len(result) == page_size:
                    return {"items": result, "next_after": list(returned_key), "has_more": True}
                result.append(item)
                returned_key = key
                continue
            if authenticated_source is None:
                source = read_authenticated_source(connection, project_id=project_id,
                    reference=reference, indexed_origin_commit=item["source_origin_project_commit"])
                if variant == "root" and reference["kind"] == "capture":
                    inventory_posting = index.posting(inventory_directory_key(
                        project_id, mission_id, scope, "root", "capture"), key)
                    if inventory_posting is None or (
                        inventory_posting.reference != posting.reference
                        or inventory_posting.payload_digest != posting.payload_digest
                        or inventory_posting.origin_commit != posting.origin_commit
                        or inventory_posting.source_origin_commit != posting.source_origin_commit
                        or {k: v for k, v in inventory_posting.metadata.items() if k != "capture_descriptor"} != dict(posting.metadata)
                    ):
                        _integrity("root Capture lexical occurrence lost its exact inventory descriptor")
                    capture_descriptor = inventory_posting.metadata.get("capture_descriptor")
                    if not isinstance(capture_descriptor, Mapping):
                        _integrity("root Capture occurrence has no authenticated metadata descriptor")
                    if root_capture_projection is not None:
                        independently_projected = root_capture_projection(source.document, source_project_commit, posting.origin_commit)
                        if canonical_json_bytes(independently_projected) != canonical_json_bytes(capture_descriptor):
                            _integrity("root Capture occurrence differs from independent same-cut projection")
                    source = next(s for s in capture_source_projections(
                        project_id=project_id, capture=source.document,
                        origin_commit=item["source_origin_project_commit"],
                        root_descriptor=capture_descriptor, projection_commit=posting.origin_commit,
                    ) if set(s.fields) == {"root"})
                    item["capture_descriptor"] = dict(capture_descriptor)
            else:
                source = authenticated_source(item)
            if source.project_id != project_id or source.mission_id != mission_id or source.reference != reference:
                _integrity("owner-content candidate failed exact source/scope authentication")
            if (source.source_origin_commit if source.source_origin_commit is not None else source.origin_commit) != item["source_origin_project_commit"]:
                _integrity("owner-content candidate source origin changed")
            if source.origin_commit != posting.origin_commit:
                _integrity("owner-content candidate projection occurrence changed")
            values = source.fields.get(variant)
            if values is None or any(query.casefold() not in values[field].casefold() for field in item["matched_fields"]):
                _integrity("owner-content positional match differs from its authorized source projection")
            if len(result) == page_size:
                return {"items": result, "next_after": list(returned_key), "has_more": True}
            result.append(item)
            returned_key = key
        return {"items": result, "next_after": None, "has_more": False}


def _mission_id(document: Mapping[str, Any], kind: str) -> str | None:
    mission = document.get("mission_id")
    if mission is None and kind == "candidate" and isinstance(document.get("author"), Mapping):
        mission = document["author"].get("mission_id")
    if mission is None and kind == "evidence" and isinstance(document.get("subject"), Mapping):
        mission = document["subject"].get("mission_id")
    return mission if isinstance(mission, str) and mission else None


def owner_source_projection(
    *, project_id: str, kind: str, identity: str, revision: int,
    payload_digest: str, origin_commit: int, document: Mapping[str, Any],
    row: Mapping[str, Any] | None = None, mission_id: str | None = None,
    selected_at_commit: int | None = None, retired_at_commit: int | None = None,
) -> SourceProjection:
    """Project authenticated/issued owner data through the existing projectors."""
    if not _searchable_owner(kind, document):
        raise _NotSearchableSource("recovery-internal Evidence has no ordinary search projection")
    mission = mission_id or _mission_id(document, kind)
    if mission is None:
        _integrity("eligible owner-content source lacks its Mission identity")
    reference = {"kind": kind, "identity": identity, "revision": revision,
                 "payload_sha256": payload_digest}
    fields = {"root": project_root_owner_fields(reference, document)}
    if kind in PROJECTION_FIELDS["delegated"]:
        formal = None
        if kind == "strategy":
            from .mission_frontier import PersistedStrategyRevision, _strategy_record
            from .mission_interface import _formal_request_projection
            if row is None:
                raise ValueError("Strategy projection requires its exact revision metadata")
            # The predecessor's branch-local capsule and conversion placeholder
            # remain exact historical sources, not Mission-wide Strategies. This
            # recognizes only those retained shapes; it does not revive their
            # writer or interpret their decisions as modern formal requests.
            legacy_keys = {"strategy_id", "mission_id", "revision"}
            legacy_shape = (
                set(document) == legacy_keys | {"placeholder"}
                and document.get("placeholder") is True
            ) or (
                set(document) == legacy_keys | {
                    "branch_id", "is_current", "lifecycle", "branch_revision",
                    "attention_state", "decision_capsule", "decision_capsule_sha256",
                }
                and isinstance(document.get("decision_capsule"), Mapping)
            )
            legacy = (
                legacy_shape and document.get("strategy_id") == identity
                and document.get("mission_id") == mission
                and type(document.get("revision")) is int
                # Predecessor complete-poststate copies advance the physical
                # typed row without reauthoring the semantic payload revision.
                and document["revision"] > 0
            )
            formal = [] if legacy else _formal_request_projection(PersistedStrategyRevision(
                record=_strategy_record(document), revision=revision,
                predecessor_revision=row["predecessor_revision"],
                created_actor=str(row["created_actor"]), created_at=str(row["created_at"]),
            ))
        fields["delegated"] = project_delegated_owner_fields(reference, document, strategy_formal_requests=formal)
    return SourceProjection(project_id, mission, kind, identity, revision,
                            payload_digest, origin_commit, document, fields,
                            selected_at_commit=(origin_commit if kind != "evidence" and selected_at_commit is None else selected_at_commit),
                            retired_at_commit=retired_at_commit)


def capture_source_projections(
    *, project_id: str, capture: Mapping[str, Any], origin_commit: int,
    root_descriptor: Mapping[str, Any] | None = None, projection_commit: int | None = None,
) -> tuple[SourceProjection, ...]:
    """Metadata only; an exact root descriptor must be supplied, never guessed."""
    mission = str(capture["mission_id"])
    identity = str(capture["capture_id"])
    fields = {"delegated": project_delegated_capture_fields(capture)}
    digest = hashlib.sha256(canonical_json_bytes(capture)).hexdigest()
    sources = [SourceProjection(project_id, mission, "capture", identity, 0, digest,
                               origin_commit, capture, fields, origin_commit)]
    if root_descriptor is not None:
        occurrence = origin_commit if projection_commit is None else projection_commit
        sources.append(SourceProjection(project_id, mission, "capture", identity,
            occurrence, digest, occurrence, capture,
            {"root": project_root_capture_fields(root_descriptor)}, origin_commit,
            inventory_metadata={"capture_descriptor": root_descriptor}))
    for artifact in capture["artifacts"]:
        artifact_identity = f"{identity}#{artifact['ordinal']}"
        artifact_document = {"capture_id": identity, "artifact": artifact}
        sources.append(SourceProjection(
            project_id, mission, "capture-artifact", artifact_identity, 0,
            hashlib.sha256(canonical_json_bytes(artifact_document)).hexdigest(), origin_commit,
            artifact_document, {"delegated": project_delegated_artifact_fields(identity, artifact)},
            origin_commit,
        ))
    return tuple(sources)


def read_authenticated_source(
    connection: sqlite3.Connection, *, project_id: str, reference: Mapping[str, Any],
    allow_legacy_fallback: bool = False, indexed_origin_commit: int | None = None,
) -> SourceProjection:
    """Exact keyed immutable-source read; never enumerates candidate owners."""
    from . import workspace_store as store
    from .workspace_schema import IdentityKind, TypedWorkspaceId
    kind, identity, revision = str(reference["kind"]), str(reference["identity"]), int(reference["revision"])
    if kind in {"capture", "capture-artifact"}:
        capture_id = identity.rsplit("#", 1)[0] if kind == "capture-artifact" else identity
        origin_row = store._validated_raw_capture_origin_row_from_connection(connection, project_id=project_id, capture_id=capture_id)
        payload = store._validated_raw_capture_read_from_connection(connection, project_id=project_id, capture_id=capture_id)
        if origin_row is None or payload is None:
            _integrity("indexed Capture source is unavailable")
        capture = {**dict(payload["record"]), "artifacts": [dict(a) for a in payload["artifacts"]]}
        sources = capture_source_projections(project_id=project_id, capture=capture, origin_commit=origin_row[1])
        source = next((s for s in sources if s.kind == kind and s.identity == identity), None)
        if source is None or source.reference != dict(reference):
            _integrity("indexed Capture descriptor differs from exact custody")
        return source
    if kind == "evidence":
        row = connection.execute("SELECT * FROM evidence_item_revision WHERE evidence_id=? AND revision=?", (identity, revision)).fetchone()
        if row is None:
            _integrity("indexed Evidence source is unavailable")
        origin = (indexed_origin_commit if indexed_origin_commit is not None else
            store._require_historical_evidence_revision_origin(connection, project_id=project_id, row=row, allow_legacy_fallback=allow_legacy_fallback))
        document = dict(store._validated_evidence_payload_from_row(connection, row))
        if not _searchable_owner(kind, document):
            raise _NotSearchableSource("recovery-internal Evidence has no ordinary search projection")
        mission = _mission_id(document, kind)
    elif kind == "capture-annotation":
        row = connection.execute("SELECT * FROM capture_scope_annotation_revision WHERE annotation_id=? AND revision=?", (identity, revision)).fetchone()
        if row is None:
            _integrity("indexed annotation source is unavailable")
        document, capture, origin = store._require_exact_annotation_dependency(connection, project_id=project_id, row=row)
        document = {**dict(document), "annotation_kind": str(row["annotation_kind"])}
        mission = str(capture["record"]["mission_id"])
    else:
        if kind not in PROJECTION_FIELDS["root"]:
            raise ValueError("unsupported indexed source kind")
        object_id = TypedWorkspaceId(IdentityKind(kind), identity)
        table, _ = store._REVISION_TABLES[object_id.kind]
        row = connection.execute(f"SELECT * FROM {table} WHERE object_id=? AND revision=?", (identity, revision)).fetchone()
        if row is None:
            _integrity("indexed typed owner source is unavailable")
        if indexed_origin_commit is not None:
            store._validate_typed_revision_row(object_id, row)
            origin = indexed_origin_commit
        else:
            origin = store._require_historical_typed_revision_origin(connection, project_id=project_id, object_id=object_id, row=row, allow_legacy_fallback=allow_legacy_fallback)
        document = json.loads(str(row["payload_json"]))
        mission = store._typed_owner_mission_scope(
            connection, project_id=project_id, object_id=object_id,
            row=row, origin_commit=origin,
        )
    if str(row["payload_digest"]) != reference["payload_sha256"]:
        _integrity("indexed owner exact source digest changed")
    selection = None
    if kind == "evidence":
        # Predecessor removal reads only the exact current selection. Retained
        # bootstrap selection metadata comes from the independent full journal
        # derivation, not this current-head convenience.
        head = store._validated_evidence_head_from_connection(connection, identity)
        if head is not None and int(head["revision"]) == revision:
            selection = int(head["project_commit_no"])
    return owner_source_projection(project_id=project_id, kind=kind, identity=identity,
        revision=revision, payload_digest=str(row["payload_digest"]), origin_commit=origin,
        document=document, row=row, mission_id=mission, selected_at_commit=selection)


def iter_authenticated_sources(
    connection: sqlite3.Connection, *, project_id: str, source_project_commit: int,
    revision_scope: str,
) -> Iterator[SourceProjection]:
    """Full source enumeration ONLY for explicit bootstrap/complete audit.

    The Capture projection owner independently replays exact descriptor changes
    once globally. Immutable delegated custody is not given those later origins.
    """
    from . import workspace_store as store
    from .workspace_schema import IdentityKind
    from dataclasses import replace
    if revision_scope not in {"retained_history", "current_at_cut"}:
        raise ValueError("source enumeration scope is unsupported")
    metadata = connection.execute("SELECT project_id,current_project_commit FROM workspace_metadata WHERE singleton=1").fetchone()
    if metadata is None or str(metadata["project_id"]) != project_id:
        _integrity("source derivation lost its exact Store project")
    if revision_scope == "current_at_cut" and source_project_commit != int(metadata["current_project_commit"]):
        _integrity("complete current source derivation requires the physical audit/bootstrap cut")
    # This is the explicit full audit/bootstrap lane. One authenticated journal
    # walk derives Evidence selection and retirement independently of row origin.
    audit = store._ACTIVE_AUDIT_JOURNAL_INDEX.get()
    if audit is not None and audit.connection is connection:
        journals = audit.journals
    else:
        journals = store._validated_transition_journal_chain(connection, project_id=project_id)
    evidence_selections: dict[tuple[str, int], int] = {}
    retirements: dict[tuple[str, str], tuple[int, int]] = {}
    for journal in journals:
        commit = int(journal.row["project_commit_no"])
        if commit > source_project_commit:
            continue
        for advance in journal.evidence_head_advances:
            key = (str(advance["evidence_id"]), int(advance["target_revision"]))
            if key in evidence_selections:
                _integrity("Evidence revision has repeated head selection")
            evidence_selections[key] = commit
        if str(journal.row["command_kind"]) == store._ROOT5_AUXILIARY_CUTOVER_COMMAND_KIND:
            authorization = json.loads(str(journal.row["authorization_json"]))
            for retired in authorization.get("retired_heads", ()):
                retirements[(str(retired["kind"]), str(retired["object_id"]))] = (int(retired["revision"]), commit)
    for kind in PROJECTION_FIELDS["root"]:
        if kind == "capture":
            continue
        if kind == "evidence":
            table, head_table, id_column = "evidence_item_revision", "evidence_item_head", "evidence_id"
        elif kind == "capture-annotation":
            table, head_table, id_column = "capture_scope_annotation_revision", "capture_scope_annotation_head", "annotation_id"
        else:
            table, head_table = store._REVISION_TABLES[IdentityKind(kind)]
            id_column = "object_id"
        if revision_scope == "retained_history":
            selectors = connection.execute(f"SELECT {id_column} AS identity, revision, payload_digest FROM {table} ORDER BY {id_column}, revision")
        else:
            selectors = connection.execute(f"SELECT {id_column} AS identity, revision, payload_digest FROM {head_table} ORDER BY {id_column}")
        for selector in selectors:
            reference = {"kind": kind, "identity": str(selector["identity"]),
                         "revision": int(selector["revision"]), "payload_sha256": str(selector["payload_digest"])}
            try:
                source = read_authenticated_source(connection, project_id=project_id,
                                                   reference=reference, allow_legacy_fallback=True)
            except _NotSearchableSource:
                continue
            if source.origin_commit > source_project_commit:
                continue
            retirement = retirements.get((kind, source.identity))
            yield replace(source,
                selected_at_commit=evidence_selections.get((source.identity, source.revision)) if kind == "evidence" else source.origin_commit,
                retired_at_commit=retirement[1] if retirement is not None and source.revision == retirement[0] else None)
    for row in connection.execute("SELECT capture_id FROM raw_capture WHERE project_id=? ORDER BY capture_id", (project_id,)):
        identity = str(row["capture_id"])
        origin_row = store._validated_raw_capture_origin_row_from_connection(connection, project_id=project_id, capture_id=identity)
        if origin_row is None or origin_row[1] > source_project_commit:
            continue
        payload = store._validated_raw_capture_read_from_connection(connection, project_id=project_id, capture_id=identity)
        if payload is None:
            _integrity("eligible Capture lost its authenticated metadata")
        capture = {**dict(payload["record"]), "artifacts": [dict(a) for a in payload["artifacts"]]}
        yield from capture_source_projections(project_id=project_id, capture=capture,
            origin_commit=origin_row[1])
    from .owner_content_capture import iter_capture_projection_occurrences
    yield from iter_capture_projection_occurrences(connection, project_id=project_id,
        source_project_commit=source_project_commit, revision_scope=revision_scope)


def _json_values(values: Mapping[str, Any]) -> dict[str, Any]:
    return {key: json.loads(value) if key.endswith("_json") and isinstance(value, str) else value
            for key, value in values.items()}


def prepare_owner_content_delta(
    connection: sqlite3.Connection, *, project_id: str, project_commit: int,
    planned_revision_rows: Sequence[tuple[Any, Any, int, Mapping[str, Any], str]],
    prepared_auxiliary: Sequence[Any], prepared_head_advances: Sequence[Mapping[str, Any]],
    retired_heads: Sequence[Mapping[str, Any]] = (),
) -> OwnerContentDelta:
    """Derive affected memberships BEFORE rows/heads are applied.

    Planned rows are issued by the sole Store writer, already schema/authority
    validated. Existing predecessors are separately authenticated. Evidence
    creation and Evidence selection remain distinct; old retained revisions can
    become a head without being indexed again as newly created history.
    Root Capture changes are finalized after the Store applies its existing
    coverage/epoch/checkpoint writes. The returned pending flag prevents sealing
    an index missing that required second phase of this same transaction.
    """
    from . import workspace_store as store
    from .workspace_schema import IdentityKind
    from dataclasses import replace
    removed: dict[tuple[str, str], SourceProjection] = {}
    retained: list[SourceProjection] = []
    current: list[SourceProjection] = []
    removed_retained: list[SourceProjection] = []
    new_by_key: dict[tuple[str, str, int], SourceProjection] = {}
    excluded_evidence: set[tuple[str, int]] = set()

    def old_head(kind: str, identity: str) -> SourceProjection | None:
        if kind == "evidence":
            table, column = "evidence_item_head", "evidence_id"
        elif kind == "capture-annotation":
            table, column = "capture_scope_annotation_head", "annotation_id"
        else:
            _, table = store._REVISION_TABLES[IdentityKind(kind)]
            column = "object_id"
        row = connection.execute(f"SELECT revision,payload_digest FROM {table} WHERE {column}=?", (identity,)).fetchone()
        if row is None:
            return None
        try:
            source = read_authenticated_source(connection, project_id=project_id, reference={
                "kind": kind, "identity": identity, "revision": int(row["revision"]),
                "payload_sha256": str(row["payload_digest"]),
            })
        except _NotSearchableSource:
            return None
        removed[(kind, identity)] = source
        return source

    def capture_metadata(capture_id: str) -> Mapping[str, Any]:
        issued = next((item for item in prepared_auxiliary if item.table.value == "raw_capture" and item.values["capture_id"] == capture_id), None)
        if issued is None:
            payload = store._validated_raw_capture_read_from_connection(connection, project_id=project_id, capture_id=capture_id)
            if payload is None:
                _integrity("indexed auxiliary owner has no exact Capture custody")
            return {**dict(payload["record"]), "artifacts": [dict(a) for a in payload["artifacts"]]}
        value = _json_values(issued.values)
        return {
            "capture_id": capture_id, "project_id": project_id,
            **{key: value[key] for key in ("mission_id", "executive_epoch_id", "capture_kind", "observation_id", "assignment_id")},
            "provenance": value["provenance_json"], "completion": value["completion_json"],
            "artifacts": [{key: item.values[key] for key in ("ordinal", "role", "logical_name", "blob_sha256")}
                          for item in sorted(prepared_auxiliary, key=lambda a: int(a.values.get("ordinal", 0)))
                          if item.table.value == "raw_capture_artifact" and item.values["capture_id"] == capture_id],
        }

    for write, payload, revision, values, _row_digest in planned_revision_rows:
        kind = write.object_id.kind.value
        if kind not in PROJECTION_FIELDS["root"]:
            continue
        old_head(kind, write.object_id.value)
        source = owner_source_projection(project_id=project_id, kind=kind,
            identity=write.object_id.value, revision=revision, payload_digest=payload.sha256,
            origin_commit=project_commit, document=json.loads(payload.text), row=values)
        retained.append(source)
        current.append(source)
        new_by_key[(kind, source.identity, revision)] = source
    for auxiliary in prepared_auxiliary:
        table = auxiliary.table.value
        values = _json_values(auxiliary.values)
        if table == "evidence_item_revision":
            identity, revision = str(values["evidence_id"]), int(values["revision"])
            roles = [(int(item.values["ordinal"]), str(item.values["role"]), str(item.values["blob_sha256"]))
                for item in prepared_auxiliary if item.table.value == "evidence_blob"
                and item.values["evidence_id"] == identity and int(item.values["evidence_revision"]) == revision]
            document = store._evidence_revision_payload_from_values(values, roles)
            if not _searchable_owner("evidence", document):
                excluded_evidence.add((identity, revision))
                continue
            mission = _mission_id(document, "evidence")
            source = owner_source_projection(project_id=project_id, kind="evidence",
                identity=identity, revision=revision, payload_digest=str(values["payload_digest"]),
                origin_commit=project_commit, document=document, mission_id=mission,
                selected_at_commit=project_commit if any(a["evidence_id"] == identity and int(a["target_revision"]) == revision for a in prepared_head_advances) else None)
            retained.append(source)
            new_by_key[("evidence", identity, revision)] = source
        elif table == "capture_scope_annotation_revision":
            identity, revision = str(values["annotation_id"]), int(values["revision"])
            old_head("capture-annotation", identity)
            capture = capture_metadata(str(values["capture_id"]))
            document = {"annotation_id": identity, "annotation_kind": values["annotation_kind"],
                        "capture_id": values["capture_id"], "exact_scope": values["exact_scope_json"],
                        "lifecycle": values["lifecycle"]}
            source = owner_source_projection(project_id=project_id, kind="capture-annotation",
                identity=identity, revision=revision, payload_digest=str(values["payload_digest"]),
                origin_commit=project_commit, document=document, mission_id=str(capture["mission_id"]))
            retained.append(source)
            current.append(source)
        elif table == "raw_capture":
            capture = capture_metadata(str(values["capture_id"]))
            sources = capture_source_projections(project_id=project_id, capture=capture, origin_commit=project_commit)
            retained.extend(sources)
            current.extend(sources)
    for advance in prepared_head_advances:
        identity = str(advance["evidence_id"])
        revision = int(advance["target_revision"])
        if (identity, revision) in excluded_evidence:
            continue
        old_head("evidence", identity)
        source = new_by_key.get(("evidence", identity, revision))
        if source is None:
            try:
                source = read_authenticated_source(connection, project_id=project_id,
                    reference={"kind": "evidence", "identity": identity, "revision": revision,
                               "payload_sha256": str(advance["target_payload_digest"])})
            except _NotSearchableSource:
                continue
            # Existing Evidence creation and subsequent head selection are two
            # journal facts. Replace only its derived retained metadata; the
            # exact scientific source and immutable earlier index roots survive.
            if source.selected_at_commit is not None:
                _integrity("Evidence head advance tried to select an already selected revision")
            removed_retained.append(source)
            source = replace(source, selected_at_commit=project_commit)
            retained.append(source)
        current.append(source)
    for retired in retired_heads:
        kind, identity = str(retired["kind"]), str(retired["object_id"])
        if kind in PROJECTION_FIELDS["root"] and kind != "capture":
            source = old_head(kind, identity)
            if source is None:
                _integrity("owner-content retirement has no exact prior current source")
            if any(s.kind == kind and s.identity == identity for s in current):
                _integrity("owner-content transaction cannot retire and advance the same head")
            if "revision" in retired and int(retired["revision"]) != source.revision:
                _integrity("owner-content retirement differs from its exact prior revision")
            if "payload_digest" in retired and str(retired["payload_digest"]) != source.payload_digest:
                _integrity("owner-content retirement differs from its exact prior digest")
            removed_retained.append(source)
            retained.append(replace(source, retired_at_commit=project_commit))
    return OwnerContentDelta(tuple(removed.values()), tuple(retained), tuple(current), tuple(removed_retained), True)


def with_capture_projection_changes(
    delta: OwnerContentDelta,
    changes: Sequence[tuple[SourceProjection | None, SourceProjection]],
) -> OwnerContentDelta:
    """Finish the Store's same-transaction Capture projection, even if empty."""
    capture_removals: list[SourceProjection] = []
    additions: list[SourceProjection] = []
    seen: set[str] = set()
    for before, after_source in changes:
        if after_source.kind != "capture" or set(after_source.fields) != {"root"}:
            raise ValueError("dynamic projection change must be an exact root Capture occurrence")
        if after_source.identity in seen:
            raise ValueError("dynamic projection repeats one affected Capture")
        seen.add(after_source.identity)
        if before is not None:
            if (before.kind != "capture" or set(before.fields) != {"root"}
                or before.reference != after_source.reference
                or before.mission_id != after_source.mission_id
                or before.project_id != after_source.project_id
                or before.origin_commit >= after_source.origin_commit):
                raise ValueError("Capture metadata successor changed immutable custody or occurrence order")
            capture_removals.append(before)
        additions.append(after_source)
    return OwnerContentDelta(delta.removed_current + tuple(capture_removals),
        delta.added_retained + tuple(additions), delta.added_current + tuple(additions),
        delta.removed_retained, False)


def source_is_current_at_cut(
    connection: sqlite3.Connection, *, project_id: str, mission_id: str,
    reference: Mapping[str, Any], cut: int, descriptor: Mapping[str, Any],
) -> bool:
    """Select historical heads using bootstrap-authenticated selection facts.

    Historical source creation is not Evidence selection. Bootstrap metadata
    records the latter from the existing exact journal; no query reconstructs
    all-owner history. A next exact owner revision is sought through an
    authenticated path, not inferred absent from a SQL candidate list.
    """
    from .owner_content_index import inventory_directory_key
    kind, identity, revision = str(reference["kind"]), str(reference["identity"]), int(reference["revision"])
    variant = "delegated" if kind in {"capture", "capture-artifact"} else "root"
    directory = inventory_directory_key(project_id, mission_id, "retained_history", variant, kind)
    index = _index(connection, descriptor)
    posting = index.posting(directory, (kind, identity, revision))
    if posting is None or dict(posting.metadata.get("reference", {})) != dict(reference):
        _integrity("historical current selection lost its exact bootstrap source")
    if posting.origin_commit > cut:
        return False
    if kind in {"capture", "capture-artifact"}:
        return True
    retired = posting.metadata.get("retired_at_commit")
    if retired is not None and retired <= cut:
        return False
    selected = posting.metadata.get("selected_at_commit")
    if selected is None or selected > cut:
        return False
    with closing(index.postings(directory, after=(kind, identity, revision))) as following:
        successor = next(following, None)
    if successor is None or successor.key[:2] != (kind, identity):
        return True
    next_selected = successor.metadata.get("selected_at_commit")
    # Evidence can have future unselected revisions, but contiguous advancement
    # cannot select a higher revision while this immediate successor is unselected.
    return next_selected is None or next_selected > cut
