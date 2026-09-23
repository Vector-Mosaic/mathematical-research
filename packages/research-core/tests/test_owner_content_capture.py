from __future__ import annotations

import json
import hashlib
from dataclasses import fields
from pathlib import Path
import sqlite3
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import patch
import weakref

PACKAGE_ROOT = Path(__file__).resolve().parents[1]
if str(PACKAGE_ROOT) not in sys.path:
    sys.path.insert(0, str(PACKAGE_ROOT))

from research_core import owner_content_capture as projection
from research_core import workspace_store as store
from research_core.json_support import canonical_json_bytes
from research_core.owner_content_projection import project_root_capture_fields


def capture(identity: str = "c", *, epoch: str | None = "epoch") -> dict:
    return {"capture_id": identity, "project_id": "p", "mission_id": "m",
            "executive_epoch_id": epoch, "capture_kind": "output",
            "observation_id": f"observation.{identity}", "assignment_id": "assignment",
            "provenance": {"parent_thread_id": "parent", "child_thread_id": "child", "root_thread_id": "root"},
            "completion": None,
            "artifacts": [{"ordinal": 0, "role": "output", "logical_name": "qualification", "blob_sha256": "a" * 64}]}


def material(*, origin: int = 1, pending: bool = True, terminal: dict | None = None) -> dict:
    record = capture()
    return {"capture_id": record["capture_id"], "record": record,
            "origin_project_commit": origin, "terminal": terminal,
            "artifacts": [{**a, "pending": pending} for a in record["artifacts"]]}


class RootCaptureDescriptorTests(unittest.TestCase):
    def test_exact_closed_descriptor_keeps_cross_json_strings_and_no_body(self) -> None:
        rendered = projection.build_capture_descriptor(material(origin=5,
            terminal={"event_kind": "failed_before_checkpoint", "project_commit_no": 4}),
            checkpoint={"project_commit_no": 3})
        self.assertEqual(rendered, {
            "id": "capture:c", "kind": "capture", "title": "assignment",
            "capture_kind": "output", "origin_epoch_id": "epoch",
            "origin_epoch_state": "failed_before_checkpoint",
            "late_classification": "after_failed_terminal",
            "cut_relation": "after_latest_checkpoint", "pending_state": "current_pending",
            "native_lineage": {"parent_thread_id": "parent", "child_thread_id": "child", "root_thread_id": "root"},
            "artifacts": [{"handle": "capture-artifact:c#0", "ordinal": 0, "role": "output", "logical_name": "qualification", "pending": True}],
            "failed_interval_or_late_output": True, "trust_class": "custody_descriptor", "completeness": "descriptor"})
        fields = project_root_capture_fields(rendered)
        serialized = canonical_json_bytes(rendered).decode("utf-8")
        self.assertIn(serialized.casefold(), tuple(value.casefold() for value in fields.values()))
        self.assertNotIn("blob_sha256", serialized)

    def test_checkpoint_retires_failed_interval_without_erasing_terminal(self) -> None:
        selected = material(origin=1, terminal={"event_kind": "failed_before_checkpoint", "project_commit_no": 3})
        before = projection.build_capture_descriptor(selected, checkpoint={"project_commit_no": 2})
        after = projection.build_capture_descriptor(selected, checkpoint={"project_commit_no": 4})
        self.assertTrue(before["failed_interval_or_late_output"])
        self.assertFalse(after["failed_interval_or_late_output"])
        self.assertEqual(after["origin_epoch_state"], "failed_before_checkpoint")
        self.assertEqual(after["cut_relation"], before["cut_relation"])


class CaptureAffectedBoundaryTests(unittest.TestCase):
    def test_checkpoint_affected_set_includes_origin_and_failed_terminal_crossings(self) -> None:
        with sqlite3.connect(":memory:") as connection:
            connection.row_factory = sqlite3.Row
            connection.executescript("""
                CREATE TABLE transition_journal(project_commit_no INTEGER, command_kind TEXT, auxiliary_writes_json TEXT);
                CREATE INDEX transition_journal_capture_commit ON transition_journal(project_commit_no, json_extract(auxiliary_writes_json, '$[#-1].primary_key.capture_id')) WHERE command_kind='commit_raw_capture';
                CREATE TABLE raw_capture(capture_id TEXT PRIMARY KEY, mission_id TEXT, executive_epoch_id TEXT, capture_kind TEXT, created_at TEXT);
                CREATE INDEX raw_capture_mission_epoch_order ON raw_capture(mission_id, executive_epoch_id, created_at, capture_id);
                CREATE TABLE executive_epoch_event(mission_id TEXT, project_commit_no INTEGER, executive_epoch_id TEXT, event_kind TEXT);
                CREATE INDEX executive_epoch_event_mission_boundary ON executive_epoch_event(mission_id, project_commit_no);
            """)
            for identity, mission, epoch, kind, origin in [
                    ("old-failed", "m", "failed", "output", 1),
                    ("old-stable", "m", "stable", "output", 2),
                    ("new", "m", "new", "output", 6),
                    ("other-mission", "other", "failed", "output", 6),
                    ("failed-assignment", "m", "failed", "assignment", 1)]:
                connection.execute("INSERT INTO raw_capture VALUES (?,?,?,?,?)", (identity, mission, epoch, kind, "t"))
                connection.execute("INSERT INTO transition_journal VALUES (?,?,?)", (origin, "commit_raw_capture",
                    json.dumps([{"primary_key": {"capture_id": identity}}])))
            connection.execute("INSERT INTO executive_epoch_event VALUES ('m',7,'failed','failed_before_checkpoint')")
            self.assertEqual(projection._checkpoint_affected(connection, mission_id="m", old_baseline=5, new_baseline=8),
                             {"old-failed", "new"})
            self.assertEqual(projection._checkpoint_affected(connection, mission_id="m", old_baseline=None, new_baseline=8),
                             {"old-failed", "old-stable", "new", "failed-assignment"})


class IssuedCaptureCustodyTests(unittest.TestCase):
    def setUp(self) -> None:
        self.connection = sqlite3.connect(":memory:")
        self.connection.row_factory = sqlite3.Row
        self.addCleanup(self.connection.close)
        self.connection.executescript("""
            CREATE TABLE raw_capture(capture_id TEXT PRIMARY KEY, project_id TEXT,
                mission_id TEXT, executive_epoch_id TEXT, capture_kind TEXT,
                observation_id TEXT, assignment_id TEXT, provenance_json TEXT,
                completion_json TEXT, created_at TEXT, row_digest TEXT);
            CREATE TABLE raw_capture_artifact(capture_id TEXT, ordinal INTEGER,
                role TEXT, logical_name TEXT, blob_sha256 TEXT,
                PRIMARY KEY(capture_id, ordinal));
        """)
        self.expected = capture(epoch=None)
        raw = {key: self.expected[key] for key in (
            "capture_id", "project_id", "mission_id", "executive_epoch_id",
            "capture_kind", "observation_id", "assignment_id")}
        raw.update(provenance_json=canonical_json_bytes(self.expected["provenance"]).decode("utf-8"),
                   completion_json=None, created_at="2026-09-14T00:00:00Z")
        artifact = {"capture_id": "c", **self.expected["artifacts"][0]}
        references = []
        for table, values in ((store.AuxiliaryTable.RAW_CAPTURE, raw),
                              (store.AuxiliaryTable.RAW_CAPTURE_ARTIFACT, artifact)):
            spec = store._AUXILIARY_SPECS[table]
            self.assertEqual(set(values), set(spec.columns))
            digest = store._row_digest(table.value, values)
            references.append({"table": table.value,
                "primary_key": {key: values[key] for key in spec.primary_key}, "row_digest": digest})
            persisted = {**values, **({"row_digest": digest} if spec.stored_row_digest else {})}
            self.connection.execute(f"INSERT INTO {table.value} ({','.join(persisted)}) VALUES ({','.join('?' for _ in persisted)})",
                                    tuple(persisted.values()))
        self.journal = SimpleNamespace(row={"command_kind": "commit_raw_capture"}, auxiliary_writes=references,
            auxiliary_writes_by_identity={(ref["table"], canonical_json_bytes(ref["primary_key"])): ref for ref in references})
        pending = projection.build_capture_descriptor({"capture_id": "c", "record": self.expected,
            "origin_project_commit": 1, "terminal": None,
            "artifacts": [{**artifact, "pending": True}]}, checkpoint=None)
        self.posting = SimpleNamespace(source_origin_commit=1, origin_commit=1,
            payload_digest=hashlib.sha256(canonical_json_bytes(self.expected)).hexdigest(),
            metadata={"capture_descriptor": pending})
        self.addCleanup(patch.stopall)
        patch.object(projection, "_current_posting", return_value=self.posting).start()
        self.read_envelope = patch.object(store, "_validated_commit_envelope", return_value=self.journal).start()

    def test_coverage_publication_authenticates_artifacts_without_stored_digest_column(self) -> None:
        self.assertNotIn("row_digest", {row["name"] for row in self.connection.execute("PRAGMA table_info(raw_capture_artifact)")})
        with patch.object(store, "_CaptureCoverageMutation", return_value=SimpleNamespace(lookup=lambda **_: 1)), \
             patch.object(projection, "_checkpoint_at_cut", return_value=None), \
             patch.object(projection, "_terminal_at_cut", return_value=None):
            changes = projection.capture_projection_changes(self.connection, project_id="p", project_commit=3,
                descriptor={}, prepared_auxiliary=(), coverage_commitment=object(),
                coverage_changes=(({"mission_id": "m", "capture_id": "c", "artifact_ordinal": 0}, 1, False),))
        self.assertEqual(len(changes), 1)
        before, after = changes[0]
        self.assertEqual(before.source_origin_commit, 1)
        self.assertEqual(after.origin_commit, 3)
        self.assertEqual(after.inventory_metadata["capture_descriptor"]["pending_state"], "fully_covered")
        self.read_envelope.assert_called_once_with(self.connection, project_id="p", commit_no=1)

    def test_artifact_mutation_and_inventory_drift_fail_against_original_journal(self) -> None:
        for sql in (
            "UPDATE raw_capture_artifact SET role='changed'",
            "UPDATE raw_capture_artifact SET logical_name='changed'",
            "UPDATE raw_capture_artifact SET blob_sha256='bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb'",
            "DELETE FROM raw_capture_artifact",
            "INSERT INTO raw_capture_artifact VALUES ('c',1,'output','extra','aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa')",
        ):
            with self.subTest(sql=sql):
                self.connection.execute("SAVEPOINT corruption")
                try:
                    self.connection.execute(sql)
                    with self.assertRaises(store.WorkspaceIntegrityError):
                        projection._issued_capture(self.connection, "c", project_id="p", descriptor={})
                finally:
                    self.connection.execute("ROLLBACK TO corruption")
                    self.connection.execute("RELEASE corruption")

    def test_exact_rows_still_require_the_sealed_combined_payload(self) -> None:
        self.posting.payload_digest = "f" * 64
        with self.assertRaisesRegex(store.WorkspaceIntegrityError, "predecessor inventory"):
            projection._issued_capture(self.connection, "c", project_id="p", descriptor={})


class CaptureOccurrenceReplayTests(unittest.TestCase):
    def test_replay_uses_write_time_evidence_columns_and_rejects_wrong_digests(self) -> None:
        table = store.AuxiliaryTable.EVIDENCE_ITEM_REVISION
        current_spec = store._ROOT6_AUXILIARY_SPECS[table]
        legacy_spec = store._ROOT5_AUXILIARY_SPECS[table]
        self.assertEqual(set(current_spec.columns) - set(legacy_spec.columns), {"project_commit_no"})
        for retained_root5 in (False, True):
            with self.subTest(retained_root5=retained_root5), sqlite3.connect(":memory:") as connection:
                connection.row_factory = sqlite3.Row
                self.addCleanup(connection.close)
                connection.execute("CREATE TABLE workspace_metadata(singleton INTEGER, project_id TEXT, root_digest_version INTEGER)")
                connection.execute("INSERT INTO workspace_metadata VALUES(1,'p',6)")
                connection.execute("CREATE TABLE evidence_item_revision (" + ",".join(current_spec.columns) + ")")
                values = {
                    "evidence_id": "evidence.fixture", "revision": 1, "subtype": "candidate_a1_closure",
                    "subject_json": '{"mission_id":"m"}', "exact_scope_json": '{}',
                    "rigor": "fixture", "limitations_json": '[]', "non_inferences_json": '[]',
                    "security_json": '{}', "retention_json": '{}', "availability_state": "verified_available",
                    "canonical_effect": "none", "payload_digest": "a" * 64, "created_at": "fixture",
                    "project_commit_no": None if retained_root5 else 1,
                }
                connection.execute("INSERT INTO evidence_item_revision VALUES(" + ",".join("?" for _ in current_spec.columns) + ")",
                                   tuple(values[column] for column in current_spec.columns))
                authored_spec = legacy_spec if retained_root5 else current_spec
                authored = {column: values[column] for column in authored_spec.columns}
                reference = {"table": table.value, "primary_key": {"evidence_id": values["evidence_id"], "revision": 1},
                             "row_digest": store._row_digest(table.value, authored)}
                journal = SimpleNamespace(row={"project_commit_no": 1, "command_kind": "commit_candidate_revision"},
                    auxiliary_writes=(reference,), evidence_head_advances=())
                token = store._ACTIVE_AUDIT_JOURNAL_INDEX.set(SimpleNamespace(connection=connection, journals=(journal,)))
                prepared = []
                original_changes = store._capture_coverage_transaction_changes

                def observe_changes(*args, **kwargs):
                    prepared.extend(kwargs["prepared_auxiliary"])
                    return original_changes(*args, **kwargs)

                try:
                    # The fixture supplies only the already-authenticated root6
                    # boundary; real selection, SQLite rows, digests and replay
                    # remain exercised. Migration-owner tests prove that boundary.
                    with patch.object(store, "_root6_start_project_commit_for_exact_envelope",
                                      return_value=3 if retained_root5 else 0) as boundary, \
                         patch.object(store, "_validated_transition_journal_chain", side_effect=AssertionError("reloaded audit history")), \
                         patch.object(store, "_capture_coverage_transaction_changes", observe_changes):
                        self.assertEqual(tuple(projection.iter_capture_projection_occurrences(
                            connection, project_id="p", source_project_commit=3)), ())
                        boundary.assert_called_once_with(connection, project_id="p")
                        self.assertEqual(len(prepared), 1)
                        self.assertEqual(prepared[0].values, authored)
                        # Neither the other generation's column interpretation
                        # nor an arbitrary digest is accepted as a fallback.
                        other = ({**authored, "project_commit_no": None} if retained_root5 else
                                 {key: value for key, value in authored.items() if key != "project_commit_no"})
                        for wrong_digest in (store._row_digest(table.value, other), "f" * 64):
                            reference["row_digest"] = wrong_digest
                            with self.assertRaisesRegex(store.WorkspaceIntegrityError, "source differs from exact journal"):
                                tuple(projection.iter_capture_projection_occurrences(
                                    connection, project_id="p", source_project_commit=3))
                finally:
                    store._ACTIVE_AUDIT_JOURNAL_INDEX.reset(token)

    def test_replay_emits_only_distinct_projections_and_covers_late_terminal_crossing(self) -> None:
        connection = sqlite3.connect(":memory:")
        connection.row_factory = sqlite3.Row
        self.addCleanup(connection.close)
        connection.execute("CREATE TABLE workspace_metadata(singleton INTEGER, project_id TEXT, root_digest_version INTEGER)")
        connection.execute("INSERT INTO workspace_metadata VALUES(1,'p',6)")
        scope = {"mission_id": "m", "capture_id": "c", "artifact_ordinal": 0}
        row = lambda table, values: SimpleNamespace(table=table, values=values)
        event_rows = {
            1: (row(store.AuxiliaryTable.RAW_CAPTURE, {"capture_id": "c"}),),
            2: (row(store.AuxiliaryTable.CONTINUATION_CHECKPOINT, {"mission_id": "m", "project_commit_no": 2}),),
            3: (row(store.AuxiliaryTable.EXECUTIVE_EPOCH_EVENT, {"mission_id": "m", "executive_epoch_id": "epoch", "event_kind": "failed_before_checkpoint", "project_commit_no": 3}),),
            4: (row(store.AuxiliaryTable.CONTINUATION_CHECKPOINT, {"mission_id": "m", "project_commit_no": 4}),),
            5: (), 6: (), 7: (), 8: (),
            9: (row(store.AuxiliaryTable.CONTINUATION_CHECKPOINT, {"mission_id": "m", "project_commit_no": 9}),
                row(store.AuxiliaryTable.RAW_CAPTURE, {"capture_id": "d"})),
        }
        deltas = {1: ((scope, 0, True),), 5: ((scope, 1, False),), 6: ((scope, 1, False),),
                  7: ((scope, -1, False),), 8: ((scope, -1, False),),
                  9: (({**scope, "capture_id": "d"}, 0, True),)}
        journals = tuple(SimpleNamespace(row={"project_commit_no": i, "command_kind": "fixture"}, evidence_head_advances=(i,)) for i in range(1, 10))
        selected_specs = []

        def event_rows_at(_connection, journal, *, auxiliary_specs):
            selected_specs.append(auxiliary_specs)
            return event_rows[journal.row["project_commit_no"]]

        token = store._ACTIVE_AUDIT_JOURNAL_INDEX.set(SimpleNamespace(connection=connection, journals=journals))
        captures = {"c": (capture(), 1), "d": (capture("d"), 9)}
        try:
            with patch.object(projection, "_journal_rows", side_effect=event_rows_at), \
                 patch.object(store, "_root6_start_project_commit_for_exact_envelope", return_value=0) as boundary, \
                 patch.object(projection, "_capture", side_effect=lambda _c, _p, identity: captures[identity]), \
                 patch.object(store, "_capture_coverage_transaction_changes", side_effect=lambda _c, **kw: deltas.get(kw["prepared_head_advances"][0], ())):
                history = tuple(projection.iter_capture_projection_occurrences(connection, project_id="p", source_project_commit=9))
                current = tuple(projection.iter_capture_projection_occurrences(connection, project_id="p", source_project_commit=9, revision_scope="current_at_cut"))
                # Replay streams real SourceProjection instances. Once another
                # Capture is yielded, a consumed owner's final occurrence must
                # not remain retained by the current-at-cut-only latest map.
                streamed = projection.iter_capture_projection_occurrences(connection, project_id="p", source_project_commit=9)
                retained = None
                try:
                    for expected in history:
                        actual = next(streamed)
                        self.assertEqual(
                            canonical_json_bytes({field.name: getattr(actual, field.name) for field in fields(actual)}),
                            canonical_json_bytes({field.name: getattr(expected, field.name) for field in fields(expected)}),
                        )
                        if actual.identity == "c" and actual.origin_commit == 8:
                            retained = weakref.ref(actual)
                        del actual
                    self.assertIsNotNone(retained)
                    self.assertIsNone(retained())
                    with self.assertRaises(StopIteration):
                        next(streamed)
                finally:
                    streamed.close()
                self.assertEqual(boundary.call_count, 3)
        finally:
            store._ACTIVE_AUDIT_JOURNAL_INDEX.reset(token)
        self.assertEqual([(s.identity, s.origin_commit) for s in history],
                         [("c", 1), ("c", 2), ("c", 3), ("c", 4), ("c", 5), ("c", 8), ("d", 9)])
        self.assertEqual([(s.identity, s.origin_commit) for s in current], [("c", 8), ("d", 9)])
        self.assertEqual(current, history[-2:])
        self.assertTrue(history[2].inventory_metadata["capture_descriptor"]["failed_interval_or_late_output"])
        self.assertFalse(history[3].inventory_metadata["capture_descriptor"]["failed_interval_or_late_output"])
        self.assertEqual([s.source_origin_commit for s in history], [1, 1, 1, 1, 1, 1, 9])
        self.assertTrue(all(s.reference["revision"] == 0 for s in history))
        self.assertEqual(len(selected_specs), 27)
        self.assertTrue(all(spec is store._ROOT6_AUXILIARY_SPECS for spec in selected_specs))


if __name__ == "__main__":
    unittest.main()
