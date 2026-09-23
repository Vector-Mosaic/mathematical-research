"""The existing root Capture descriptor and its Store-owned change projection.

This is metadata projection, never capture-body indexing. Ordinary maintenance
visits new captures, exact coverage scopes, an affected epoch, and checkpoint
boundary crossings. Complete occurrence reconstruction is an explicit bootstrap
or audit operation; it is not a query fallback or a second history authority.
"""
from __future__ import annotations

from bisect import bisect_right
from collections.abc import Iterator, Mapping, Sequence
from typing import Any
import hashlib
import sqlite3

from .json_support import canonical_json_bytes


def _integrity(message: str) -> None:
    from .workspace_store import WorkspaceIntegrityError
    raise WorkspaceIntegrityError(message)


def build_capture_descriptor(
    material: Mapping[str, Any], *, checkpoint: Mapping[str, Any] | None,
) -> dict[str, Any]:
    """The root retrieval descriptor, including its complete searchable JSON.

    Keep this as the single renderer used both by root retrieval and its index.
    Field boundaries, booleans, lineage and classifications are not a narrowed
    replacement for the outward descriptor's literal substring predicate.
    """
    from .executive_orientation import failed_output_is_relevant
    from .mission_interface import MissionInterface

    record = material["record"]
    capture_id = str(material["capture_id"])
    origin_commit = int(material["origin_project_commit"])
    epoch = record["executive_epoch_id"]
    terminal = material["terminal"]
    terminal_kind = None if terminal is None else str(terminal["event_kind"])
    terminal_commit = None if terminal is None else int(terminal["project_commit_no"])
    baseline = 0 if checkpoint is None else int(checkpoint["project_commit_no"])
    late = (
        "unbound_to_epoch" if epoch is None else "epoch_not_terminal"
        if terminal is None else "at_or_before_terminal"
        if origin_commit <= terminal_commit else "after_failed_terminal"
        if terminal_kind == "failed_before_checkpoint" else "after_checkpoint_terminal"
    )
    artifacts = [
        {"handle": f"capture-artifact:{capture_id}#{item['ordinal']}",
         "ordinal": int(item["ordinal"]), "role": str(item["role"]),
         "logical_name": str(item["logical_name"]), "pending": bool(item["pending"])}
        for item in material["artifacts"]
    ]
    pending = any(item["pending"] for item in artifacts)
    lineage = MissionInterface._native_capture_lineage(record)
    root_thread = record.get("provenance", {}).get("root_thread_id")
    if isinstance(root_thread, str) and root_thread:
        lineage = {**(lineage or {}), "root_thread_id": root_thread}
    return {
        "id": f"capture:{capture_id}", "kind": "capture",
        "title": str(record["assignment_id"]), "capture_kind": str(record["capture_kind"]),
        "origin_epoch_id": epoch,
        "origin_epoch_state": terminal_kind or ("unbound_to_epoch" if epoch is None else "epoch_not_terminal"),
        "late_classification": late,
        "cut_relation": "no_checkpoint" if checkpoint is None else
            "at_or_before_latest_checkpoint" if origin_commit <= baseline else "after_latest_checkpoint",
        "pending_state": "current_pending" if pending else "fully_covered",
        "native_lineage": lineage, "artifacts": artifacts,
        "failed_interval_or_late_output": failed_output_is_relevant(
            capture_kind=str(record["capture_kind"]), origin_commit=origin_commit,
            terminal_kind=terminal_kind, terminal_commit=terminal_commit,
            baseline_commit=baseline, pending=pending),
        "trust_class": "custody_descriptor", "completeness": "descriptor",
    }


def _checkpoint_at_cut(connection: sqlite3.Connection, project_id: str,
                       mission_id: str, cut: int) -> Mapping[str, Any] | None:
    from . import workspace_store as store
    row = connection.execute(
        "SELECT checkpoint_id FROM continuation_checkpoint "
        "WHERE mission_id=? AND project_commit_no<=? "
        "ORDER BY project_commit_no DESC, checkpoint_id DESC LIMIT 1", (mission_id, cut),
    ).fetchone()
    if row is None:
        return None
    return store._read_continuation_checkpoint_from_connection(
        connection, checkpoint_id=str(row["checkpoint_id"]), mission_id=mission_id,
        _journal_origin_project_id=project_id)


def _terminal_at_cut(connection: sqlite3.Connection, project_id: str,
                     capture: Mapping[str, Any], cut: int) -> Mapping[str, Any] | None:
    from . import workspace_store as store
    epoch = capture["executive_epoch_id"]
    if epoch is None:
        return None
    chain = store._read_executive_epoch_events_from_connection(
        connection, executive_epoch_id=str(epoch), mission_id=str(capture["mission_id"]))
    # Newly prepared terminal rows can already be physical during the writer's
    # projection phase; their authority is supplied separately as issued rows.
    prior_chain = tuple(row for row in chain if int(row["project_commit_no"]) <= cut)
    store._require_root_retrieval_epoch_chain_origins(
        connection, project_id=project_id, chain=prior_chain)
    return next((row for row in reversed(prior_chain)
                 if row["event_kind"] in {"checkpointed", "failed_before_checkpoint"}), None)


def _capture(connection: sqlite3.Connection, project_id: str,
             capture_id: str) -> tuple[dict[str, Any], int]:
    from . import workspace_store as store
    payload = store._validated_raw_capture_read_from_connection(
        connection, project_id=project_id, capture_id=capture_id)
    origin = store._validated_raw_capture_origin_row_from_connection(
        connection, project_id=project_id, capture_id=capture_id)
    if payload is None or origin is None:
        _integrity("root Capture projection lost exact immutable custody")
    return {**dict(payload["record"]), "artifacts": [dict(a) for a in payload["artifacts"]]}, int(origin[1])


def _descriptor(capture: Mapping[str, Any], origin: int, *, coverage: Any,
                checkpoint: Mapping[str, Any] | None,
                terminal: Mapping[str, Any] | None) -> dict[str, Any]:
    artifacts = []
    for artifact in capture["artifacts"]:
        count = coverage.lookup(mission_id=str(capture["mission_id"]),
                                capture_id=str(capture["capture_id"]),
                                artifact_ordinal=int(artifact["ordinal"]))
        if count is None:
            _integrity("root Capture projection lost exact-cut artifact coverage")
        artifacts.append({**dict(artifact), "pending": count == 0})
    return build_capture_descriptor(
        {"capture_id": capture["capture_id"], "record": capture,
         "origin_project_commit": origin, "artifacts": artifacts, "terminal": terminal},
        checkpoint=checkpoint)


def _descriptor_at_cut(connection: sqlite3.Connection, *, project_id: str,
                       capture: Mapping[str, Any], origin: int, cut: int) -> dict[str, Any]:
    from . import workspace_store as store
    coverage = store._CaptureCoverageMutation(connection, project_id=project_id,
        commitment=store._capture_coverage_commitment_at_cut(
            connection, project_id=project_id, project_commit_no=cut))
    return _descriptor(capture, origin, coverage=coverage,
        checkpoint=_checkpoint_at_cut(connection, project_id, str(capture["mission_id"]), cut),
        terminal=_terminal_at_cut(connection, project_id, capture, cut))


def capture_projection_at_cut(
    connection: sqlite3.Connection, *, project_id: str, capture: Mapping[str, Any],
    source_project_commit: int, occurrence_origin_commit: int,
) -> tuple[Mapping[str, Any], int]:
    """Authenticate a selected index occurrence against exact-cut source owners.

    The authenticated posting supplies the occurrence, never a guessed current
    owner revision. Query cost is keyed custody, epoch, checkpoint and coverage
    paths; it does not search history to rediscover the posting's occurrence.
    Complete relation audit separately proves occurrence completeness/origins.
    """
    actual, origin = _capture(connection, project_id, str(capture["capture_id"]))
    if (canonical_json_bytes(actual) != canonical_json_bytes(capture)
            or not origin <= occurrence_origin_commit <= source_project_commit):
        _integrity("indexed Capture occurrence escaped exact custody or selected cut")
    return (_descriptor_at_cut(connection, project_id=project_id, capture=actual,
                              origin=origin, cut=source_project_commit), occurrence_origin_commit)


def _root_source(project_id: str, capture: Mapping[str, Any], origin: int,
                 descriptor: Mapping[str, Any], occurrence: int) -> Any:
    from .owner_content_access import capture_source_projections
    return next(s for s in capture_source_projections(
        project_id=project_id, capture=capture, origin_commit=origin,
        root_descriptor=descriptor, projection_commit=occurrence) if "root" in s.fields)


def _current_posting(connection: sqlite3.Connection, *, project_id: str,
                     mission_id: str, capture_id: str, descriptor: Mapping[str, Any]) -> Any:
    from .owner_content_access import _index
    from .owner_content_index import inventory_directory_key
    key = inventory_directory_key(project_id, mission_id, "current_at_cut", "root", "capture")
    values = _index(connection, descriptor).postings(key, after=("capture", capture_id, -1))
    posting = next(values, None)
    if posting is None or posting.key[:2] != ("capture", capture_id):
        _integrity("affected Capture has no authenticated current projection")
    return posting


def _issued_capture(connection: sqlite3.Connection, capture_id: str, *,
                    project_id: str, descriptor: Mapping[str, Any]) -> tuple[dict[str, Any], Any]:
    """Exact metadata rows whose custody is bound by a sealed index posting.

    Unlike the public raw-Capture read this does not require the *new*, already
    physical terminal event to have its not-yet-written journal. The predecessor
    posting seals the immutable payload and source origin. Artifact rows do not
    store a row_digest column: their computed digests and complete inventory are
    checked against that authenticated original Capture journal instead.
    """
    from . import workspace_store as store
    row = connection.execute("SELECT * FROM raw_capture WHERE capture_id=?", (capture_id,)).fetchone()
    if row is None:
        _integrity("indexed Capture lost its immutable custody row")
    posting = _current_posting(connection, project_id=project_id,
        mission_id=str(row["mission_id"]), capture_id=capture_id, descriptor=descriptor)
    if row["project_id"] != project_id or posting.source_origin_commit is None:
        _integrity("affected Capture lost its authenticated predecessor scope or origin")
    journal = store._validated_commit_envelope(connection, project_id=project_id,
                                               commit_no=int(posting.source_origin_commit))
    if journal is None or journal.row["command_kind"] != "commit_raw_capture":
        _integrity("affected Capture lost its exact custody transition")
    store._require_auxiliary_reference(journal, store.AuxiliaryTable.RAW_CAPTURE, row)
    artifacts = tuple(connection.execute(
        "SELECT * FROM raw_capture_artifact WHERE capture_id=? ORDER BY ordinal", (capture_id,)))
    expected_ordinals = {
        int(ref["primary_key"]["ordinal"]) for ref in journal.auxiliary_writes
        if ref["table"] == store.AuxiliaryTable.RAW_CAPTURE_ARTIFACT.value
        and ref["primary_key"].get("capture_id") == capture_id
    }
    if expected_ordinals != {int(artifact["ordinal"]) for artifact in artifacts}:
        _integrity("indexed Capture artifact inventory differs from exact custody")
    for artifact in artifacts:
        store._require_auxiliary_reference(journal, store.AuxiliaryTable.RAW_CAPTURE_ARTIFACT, artifact)
    payload = store._raw_capture_payload_from_rows(row, artifacts)
    capture = {**dict(payload["record"]), "artifacts": [dict(a) for a in payload["artifacts"]]}
    if hashlib.sha256(canonical_json_bytes(capture)).hexdigest() != posting.payload_digest:
        _integrity("affected Capture differs from authenticated predecessor inventory")
    return capture, posting


def _checkpoint_affected(connection: sqlite3.Connection, *, mission_id: str,
                         old_baseline: int | None, new_baseline: int) -> set[str]:
    """Exact boundary changes, not all historical captures on every checkpoint."""
    if old_baseline is None:
        # The first checkpoint really changes no_checkpoint on every prior
        # descriptor. This unavoidable one-time fanout is not hidden.
        return {str(row["capture_id"]) for row in connection.execute(
            "SELECT json_extract(j.auxiliary_writes_json, '$[#-1].primary_key.capture_id') AS capture_id "
            "FROM transition_journal AS j INDEXED BY transition_journal_capture_commit "
            "JOIN raw_capture AS c ON c.capture_id=json_extract(j.auxiliary_writes_json, '$[#-1].primary_key.capture_id') "
            "WHERE j.command_kind='commit_raw_capture' AND j.project_commit_no<=? AND c.mission_id=?",
            (new_baseline, mission_id))}
    result = {str(row["capture_id"]) for row in connection.execute(
        "SELECT json_extract(j.auxiliary_writes_json, '$[#-1].primary_key.capture_id') AS capture_id "
        "FROM transition_journal AS j INDEXED BY transition_journal_capture_commit "
        "JOIN raw_capture AS c ON c.capture_id=json_extract(j.auxiliary_writes_json, '$[#-1].primary_key.capture_id') "
        "WHERE j.command_kind='commit_raw_capture' AND j.project_commit_no>? "
        "AND j.project_commit_no<=? AND c.mission_id=?", (old_baseline, new_baseline, mission_id))}
    # A failed output predating the old baseline can lose relevance when its
    # terminal (rather than its custody origin) crosses this baseline.
    for terminal in connection.execute(
        "SELECT executive_epoch_id FROM executive_epoch_event "
        "WHERE mission_id=? AND project_commit_no>? AND project_commit_no<=? "
        "AND event_kind='failed_before_checkpoint'", (mission_id, old_baseline, new_baseline)):
        result.update(str(row["capture_id"]) for row in connection.execute(
            "SELECT capture_id FROM raw_capture INDEXED BY raw_capture_mission_epoch_order "
            "WHERE mission_id=? AND executive_epoch_id=? AND capture_kind='output'",
            (mission_id, terminal["executive_epoch_id"])))
    return result


def capture_projection_changes(
    connection: sqlite3.Connection, *, project_id: str, project_commit: int,
    descriptor: Mapping[str, Any], prepared_auxiliary: Sequence[Any],
    coverage_changes: Sequence[Any], coverage_commitment: Any,
) -> tuple[tuple[Any | None, Any], ...]:
    """Finalize root-only changes after issued rows/coverage, before index seal.

    The Store remains the sole writer. Incoming rows are already authorized by
    its command validation, not read as though their journal were sealed. Old
    sources use the authenticated predecessor descriptor and exact prior cut.
    """
    from . import workspace_store as store
    prior = project_commit - 1
    new_captures: dict[str, Mapping[str, Any]] = {}
    terminals: dict[str, Mapping[str, Any]] = {}
    checkpoints: dict[str, Mapping[str, Any]] = {}
    affected = {str(scope["capture_id"]) for scope, _delta, _create in coverage_changes}
    for prepared in prepared_auxiliary:
        row = prepared.values
        if prepared.table is store.AuxiliaryTable.RAW_CAPTURE:
            artifacts = [item.values for item in prepared_auxiliary
                         if item.table is store.AuxiliaryTable.RAW_CAPTURE_ARTIFACT
                         and item.values["capture_id"] == row["capture_id"]]
            payload = store._raw_capture_payload_from_rows({**dict(row), "row_digest": prepared.row_digest}, artifacts)
            capture = {**dict(payload["record"]), "artifacts": [dict(a) for a in payload["artifacts"]]}
            new_captures[str(row["capture_id"])] = capture
            affected.add(str(row["capture_id"]))
        elif prepared.table is store.AuxiliaryTable.EXECUTIVE_EPOCH_EVENT and row["event_kind"] in {"checkpointed", "failed_before_checkpoint"}:
            terminals[str(row["executive_epoch_id"])] = row
            affected.update(str(c["capture_id"]) for c in connection.execute(
                "SELECT capture_id FROM raw_capture INDEXED BY raw_capture_mission_epoch_order "
                "WHERE mission_id=? AND executive_epoch_id=?", (row["mission_id"], row["executive_epoch_id"])))
        elif prepared.table is store.AuxiliaryTable.CONTINUATION_CHECKPOINT:
            mission = str(row["mission_id"])
            old = _checkpoint_at_cut(connection, project_id, mission, prior)
            affected.update(_checkpoint_affected(connection, mission_id=mission,
                old_baseline=None if old is None else int(old["project_commit_no"]),
                new_baseline=project_commit))
            checkpoints[mission] = row
    if not affected:
        return ()
    coverage = store._CaptureCoverageMutation(connection, project_id=project_id,
                                              commitment=coverage_commitment)
    changes = []
    for identity in sorted(affected):
        capture = new_captures.get(identity)
        if capture is None:
            capture, posting = _issued_capture(connection, identity,
                                               project_id=project_id, descriptor=descriptor)
            origin = int(posting.source_origin_commit)
            before = posting.metadata.get("capture_descriptor")
            if not isinstance(before, Mapping):
                _integrity("affected Capture lost its sealed predecessor descriptor")
            old_source = _root_source(project_id, capture, origin, before, int(posting.origin_commit))
        else:
            origin = project_commit
            before = None
            old_source = None
        mission = str(capture["mission_id"])
        checkpoint = checkpoints.get(mission)
        if checkpoint is None:
            checkpoint = _checkpoint_at_cut(connection, project_id, mission, prior)
        terminal = terminals.get(str(capture["executive_epoch_id"]))
        if terminal is None:
            terminal = _terminal_at_cut(connection, project_id, capture, prior)
        after = _descriptor(capture, origin, coverage=coverage, checkpoint=checkpoint, terminal=terminal)
        if before != after:
            changes.append((old_source, _root_source(project_id, capture, origin, after, project_commit)))
    return tuple(changes)


class _ReplayCoverage:
    def __init__(self) -> None:
        self.counts: dict[tuple[str, str, int], int] = {}

    def lookup(self, *, mission_id: str, capture_id: str, artifact_ordinal: int) -> int | None:
        return self.counts.get((mission_id, capture_id, artifact_ordinal))

    def apply(self, changes: Sequence[Any]) -> set[str]:
        affected = set()
        for scope, delta, create in changes:
            key = (str(scope["mission_id"]), str(scope["capture_id"]), int(scope["artifact_ordinal"]))
            if create:
                if key in self.counts:
                    _integrity("Capture occurrence replay repeated custody creation")
                self.counts[key] = 0
            elif key not in self.counts or self.counts[key] + delta < 0:
                _integrity("Capture occurrence replay has invalid coverage delta")
            else:
                self.counts[key] += delta
            affected.add(key[1])
        return affected


def _journal_rows(connection: sqlite3.Connection, journal: Any, *,
                  auxiliary_specs: Mapping[Any, Any]) -> tuple[Any, ...]:
    """Read exact relevant rows issued by one already authenticated journal."""
    from . import workspace_store as store
    relevant = {store.AuxiliaryTable.RAW_CAPTURE, store.AuxiliaryTable.RAW_CAPTURE_ARTIFACT,
        store.AuxiliaryTable.EXECUTIVE_EPOCH_EVENT, store.AuxiliaryTable.CONTINUATION_CHECKPOINT,
        store.AuxiliaryTable.EVIDENCE_ITEM_REVISION, store.AuxiliaryTable.EVIDENCE_CAPTURE_SOURCE,
        store.AuxiliaryTable.CAPTURE_SCOPE_ANNOTATION_REVISION}
    rows = []
    for ref in journal.auxiliary_writes:
        table = store.AuxiliaryTable(ref["table"])
        if table not in relevant:
            continue
        if table not in auxiliary_specs:
            _integrity("Capture occurrence replay source has no write-time row contract")
        spec = auxiliary_specs[table]
        primary = ref["primary_key"]
        row = connection.execute(f"SELECT * FROM {table.value} WHERE " +
            " AND ".join(f"{column}=?" for column in primary), tuple(primary.values())).fetchone()
        if row is None:
            _integrity("Capture occurrence replay lost journaled source row")
        values = {column: row[column] for column in spec.columns}
        if store._row_digest(table.value, values) != ref["row_digest"]:
            _integrity("Capture occurrence replay source differs from exact journal")
        rows.append(store._PreparedAuxiliary(table, values, str(ref["row_digest"])))
    return tuple(rows)


def iter_capture_projection_occurrences(
    connection: sqlite3.Connection, *, project_id: str, source_project_commit: int,
    revision_scope: str = "retained_history",
) -> Iterator[Any]:
    """Full bootstrap/audit rederivation; one event walk plus affected work.

    Old schema cuts are reconstructed from their authenticated source events,
    not interpreted using later coverage roots. Memory is proportional to the
    current capture inventory during this explicitly whole-history operation.
    No per-capture journal scan and no all-capture scan per ordinary epoch.
    """
    from . import workspace_store as store
    if revision_scope not in {"retained_history", "current_at_cut"}:
        raise ValueError("unsupported Capture occurrence scope")
    audit = store._ACTIVE_AUDIT_JOURNAL_INDEX.get()
    journals = (audit.journals if audit is not None and audit.connection is connection
                else store._validated_transition_journal_chain(connection, project_id=project_id))
    metadata = connection.execute(
        "SELECT project_id,root_digest_version FROM workspace_metadata WHERE singleton=1"
    ).fetchone()
    if metadata is None or str(metadata["project_id"]) != project_id:
        _integrity("Capture occurrence replay lost its exact Store project")
    root_version = int(metadata["root_digest_version"])
    current_specs = (store._ROOT6_AUXILIARY_SPECS if root_version == 6 else
                     store._ROOT5_AUXILIARY_SPECS if root_version == 5 else
                     store._ROOT4_AUXILIARY_SPECS)
    # Resolve the authenticated generation boundaries once for this whole-history
    # operation, not once per journal. A schema10-added NULL Evidence pointer is
    # not part of an older root5 row's authenticated bytes.
    root5_cutover = store._root5_auxiliary_cutover_project_commit(journals)
    root6_start = (store._root6_start_project_commit_for_exact_envelope(
        connection, project_id=project_id) if root_version == 6 else None)
    captures: dict[str, tuple[Mapping[str, Any], int]] = {}
    epoch_captures: dict[tuple[str, str], set[str]] = {}
    mission_origins: dict[str, list[tuple[int, str]]] = {}
    failed_terminals: dict[str, list[tuple[int, str]]] = {}
    terminals: dict[str, Mapping[str, Any]] = {}
    checkpoints: dict[str, Mapping[str, Any]] = {}
    latest: dict[str, Any] = {}
    descriptors: dict[str, Mapping[str, Any]] = {}
    coverage = _ReplayCoverage()
    for journal in journals:
        commit = int(journal.row["project_commit_no"])
        if commit > source_project_commit:
            break
        auxiliary_specs = store._auxiliary_specs_for_journal(
            journal, current_root_digest_version=root_version, current_specs=current_specs,
            root5_cutover_project_commit=root5_cutover, root6_start_project_commit=root6_start,
        )
        rows = _journal_rows(connection, journal, auxiliary_specs=auxiliary_specs)
        changes = store._capture_coverage_transaction_changes(connection,
            project_id=project_id, prepared_auxiliary=rows,
            prepared_head_advances=journal.evidence_head_advances)
        affected = coverage.apply(changes)
        for prepared in rows:
            row = prepared.values
            if prepared.table is store.AuxiliaryTable.RAW_CAPTURE:
                capture, origin = _capture(connection, project_id, str(row["capture_id"]))
                if origin != commit:
                    _integrity("Capture occurrence replay disagrees with custody origin")
                identity, mission = str(capture["capture_id"]), str(capture["mission_id"])
                captures[identity] = (capture, origin)
                mission_origins.setdefault(mission, []).append((origin, identity))
                if capture["executive_epoch_id"] is not None:
                    epoch_captures.setdefault((mission, str(capture["executive_epoch_id"])), set()).add(identity)
                affected.add(identity)
            elif prepared.table is store.AuxiliaryTable.EXECUTIVE_EPOCH_EVENT and row["event_kind"] in {"checkpointed", "failed_before_checkpoint"}:
                epoch, mission = str(row["executive_epoch_id"]), str(row["mission_id"])
                terminals[epoch] = row
                affected.update(epoch_captures.get((mission, epoch), ()))
                if row["event_kind"] == "failed_before_checkpoint":
                    failed_terminals.setdefault(mission, []).append((commit, epoch))
            elif prepared.table is store.AuxiliaryTable.CONTINUATION_CHECKPOINT:
                mission = str(row["mission_id"])
                old = checkpoints.get(mission)
                origins = mission_origins.get(mission, [])
                start = 0 if old is None else bisect_right(origins, (int(old["project_commit_no"]), chr(0x10ffff)))
                end = bisect_right(origins, (commit, chr(0x10ffff)))
                affected.update(identity for _origin, identity in origins[start:end])
                if old is not None:
                    failed = failed_terminals.get(mission, [])
                    start = bisect_right(failed, (int(old["project_commit_no"]), chr(0x10ffff)))
                    end = bisect_right(failed, (commit, chr(0x10ffff)))
                    affected.update(identity for _terminal, epoch in failed[start:end]
                        for identity in epoch_captures.get((mission, epoch), ())
                        if captures[identity][0]["capture_kind"] == "output")
                checkpoints[mission] = row
        for identity in sorted(affected):
            if identity not in captures:
                _integrity("Capture occurrence event precedes immutable custody")
            capture, origin = captures[identity]
            descriptor = _descriptor(capture, origin, coverage=coverage,
                checkpoint=checkpoints.get(str(capture["mission_id"])),
                terminal=terminals.get(str(capture["executive_epoch_id"])))
            if descriptors.get(identity) == descriptor:
                continue
            descriptors[identity] = descriptor
            source = _root_source(project_id, capture, origin, descriptor, commit)
            if revision_scope == "retained_history":
                yield source
            else:
                latest[identity] = source
    if revision_scope == "current_at_cut":
        yield from (latest[identity] for identity in sorted(latest))
