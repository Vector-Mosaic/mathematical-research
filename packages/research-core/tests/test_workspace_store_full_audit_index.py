from __future__ import annotations

import sqlite3
import sys
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

PACKAGE_ROOT = Path(__file__).resolve().parents[1]
TEST_ROOT = Path(__file__).resolve().parent
for candidate in (PACKAGE_ROOT, TEST_ROOT):
    if str(candidate) not in sys.path:
        sys.path.insert(0, str(candidate))

from research_core import workspace_store as store_module  # noqa: E402
from research_core.canonical_snapshot import load_canonical_snapshot  # noqa: E402
from research_core.evidence_store import EvidenceCAS, EvidenceItemRevision, EvidenceStoreError, verify_closure_manifest  # noqa: E402
from research_core.workspace_recovery import create_bound_backup, verify_portable_backup_set  # noqa: E402
from research_core.workspace_store import AuxiliaryTable, WorkspaceIntegrityError, WorkspaceStore  # noqa: E402
from test_workspace_store_root6_asymptotics import _HistoryFixture, REPO_ROOT  # noqa: E402
from test_evidence_store import valid_closure  # noqa: E402


class FullAuditIndexTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        loaded = load_canonical_snapshot(REPO_ROOT, source_commit="a" * 40)
        if not loaded.ok or loaded.value is None:
            raise AssertionError(loaded.failure)
        cls.canonical = loaded.value

    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.fixture = _HistoryFixture(Path(temporary.name) / "workspace", self.canonical)
        self.fixture.grow_to(10)

    def test_full_audit_validates_journal_chain_once(self):
        with (
            patch.object(
                store_module, "_validated_transition_journal_chain",
                wraps=store_module._validated_transition_journal_chain,
            ) as chain,
            patch.object(
                store_module, "_closure_security_scope_index",
                wraps=store_module._closure_security_scope_index,
            ) as security_scope,
        ):
            report = self.fixture.store.verify_integrity()
        self.assertEqual(report.current_project_commit, 10)
        self.assertEqual(chain.call_count, 1)
        self.assertEqual(security_scope.call_count, 1)

    def test_all_exact_auxiliary_origins_reuse_one_validated_chain(self):
        with self.fixture.store._connection(read_only=True) as connection:
            connection.execute("BEGIN")
            with patch.object(
                store_module, "_validated_transition_journal_chain",
                wraps=store_module._validated_transition_journal_chain,
            ) as chain:
                index = store_module._validated_journal_index(connection, project_id="project.rh")
                spec = store_module._ROOT6_AUXILIARY_SPECS[AuxiliaryTable.RAW_CAPTURE]
                captures = tuple(connection.execute("SELECT * FROM raw_capture"))
                self.assertGreater(len(captures), 1)
                for row in captures:
                    origin = store_module._journaled_auxiliary_origin(
                        connection, project_id="project.rh", table=AuxiliaryTable.RAW_CAPTURE,
                        row_values={column: row[column] for column in spec.columns},
                        auxiliary_specs=store_module._ROOT6_AUXILIARY_SPECS,
                        journal_index=index,
                    )
                    self.assertEqual(origin.command_kind, "commit_raw_capture")
                self.assertEqual(chain.call_count, 1)

    def _advance_mutable_heads(self, revision, evidence_digest=None, annotation_digest=None):
        capture_id = self.fixture.captures[0]
        record = EvidenceItemRevision(
            evidence_id="evidence.audit-head", revision=revision, subtype="research_observation",
            subject={"mission_id": "mission.1", "statement": f"Scoped observation revision {revision}."},
            exact_scope="The exact synthetic capture artifact.", rigor="observation_only",
            limitations=("Synthetic operational fixture.",), non_inferences=("No mathematical conclusion.",),
            security_classification="workspace_internal", retention="mission_evidence_meaning",
            blob_roles=(), availability_state="verified_available",
        )
        self.fixture.store.commit_evidence_meaning_revision(
            executive_epoch_id=self.fixture.epoch_id, record=record,
            sources=({"source_ordinal": 0, "capture_id": capture_id, "artifact_ordinal": 0,
                "exact_scope": {"scope": "Complete synthetic artifact."}},),
            expected_head_revision=None if revision == 1 else revision - 1,
            expected_head_payload_digest=evidence_digest,
            lease=self.fixture.lease, command_id=f"audit.evidence.{revision}", actor="root6-fixture",
            expected_canonical_authority_digest=self.fixture.authority_digest,
        )
        self.fixture.store.commit_capture_scope_annotation_revision(
            executive_epoch_id=self.fixture.epoch_id, annotation_id="annotation.audit-head", capture_id=capture_id,
            exact_scope={"scope": f"Exact inspected scope revision {revision}."}, lifecycle="active",
            created_epoch_id=self.fixture.epoch_id,
            expected_head_revision=None if revision == 1 else revision - 1,
            expected_head_payload_digest=annotation_digest, lease=self.fixture.lease,
            command_id=f"audit.annotation.{revision}", actor="root6-fixture",
            expected_canonical_authority_digest=self.fixture.authority_digest,
        )
        with self.fixture.store._connection(read_only=True) as connection:
            evidence = connection.execute("SELECT payload_digest FROM evidence_item_head WHERE evidence_id = 'evidence.audit-head'").fetchone()[0]
            annotation = connection.execute("SELECT payload_digest FROM capture_scope_annotation_head WHERE annotation_id = 'annotation.audit-head'").fetchone()[0]
        return evidence, annotation

    def test_full_audit_replays_evidence_and_mutable_annotation_heads(self):
        digests = self._advance_mutable_heads(1)
        self._advance_mutable_heads(2, *digests)
        report = self.fixture.store.verify_integrity()
        self.assertEqual(report.current_project_commit, 14)
        # Corrupt an unrelated old current annotation projection after a later write.
        self.fixture.commit = report.current_project_commit
        self.fixture.append_branch()
        with closing(sqlite3.connect(self.fixture.paths.database)) as connection:
            connection.execute("UPDATE capture_scope_annotation_head SET project_commit_no = 1 WHERE annotation_id = 'annotation.audit-head'")
            connection.commit()
        reopened = WorkspaceStore.open(self.fixture.paths)
        with self.assertRaisesRegex(WorkspaceIntegrityError, "annotation current head differs"):
            reopened.verify_integrity()

    def test_portable_backup_full_audit_covers_progressed_root6_semantics(self):
        digests = self._advance_mutable_heads(1)
        self._advance_mutable_heads(2, *digests)
        metadata = self.fixture.store.read_metadata()
        current_commit = int(metadata["current_project_commit"])
        current_root = str(metadata["current_root_digest"])
        candidate = self.fixture.store.read_candidate_revision(
            mission_id="mission.1",
            candidate_id="candidate.root6.current-a1",
        )
        a1 = self.fixture.store.read_candidate_a1_binding(
            candidate_id="candidate.root6.current-a1",
            candidate_revision=int(candidate["revision"]),
            candidate_digest=str(candidate["payload_digest"]),
        )

        with patch(
            "research_core.workspace_recovery._git_head",
            return_value="a" * 40,
        ):
            backup = create_bound_backup(
                store=self.fixture.store,
                cas=EvidenceCAS(self.fixture.paths),
                backup_id="root6-progressed-portable-audit",
                authority_repo_root=REPO_ROOT,
                closure_id=a1.closure_id,
            )

        portable = verify_portable_backup_set(backup.path)
        self.assertEqual(backup.manifest.store.schema_version, 10)
        self.assertEqual(backup.manifest.store.root_digest_version, 6)
        self.assertEqual(backup.manifest.store.project_commit_id, current_commit)
        self.assertEqual(portable.project_commit, current_commit)
        self.assertEqual(portable.project_root_digest, current_root)
        self.assertGreaterEqual(portable.cas_object_count, 2)
        self.assertTrue(portable.structural_verification_passed)
        self.assertTrue(portable.semantic_verification_passed)

    def test_reused_audit_index_authenticates_historical_evidence_without_timestamp_rescans(self):
        digests = self._advance_mutable_heads(1)
        digests = self._advance_mutable_heads(2, *digests)
        self._advance_mutable_heads(3, *digests)
        with self.fixture.store._connection(read_only=True) as connection:
            connection.execute("BEGIN")
            with patch.object(
                store_module,
                "_validated_journal_index",
                wraps=store_module._validated_journal_index,
            ) as build_index:
                index = store_module._validated_journal_index(
                    connection,
                    project_id=self.fixture.store.project_id,
                )
                token = store_module._ACTIVE_AUDIT_JOURNAL_INDEX.set(index)
                statements = []
                connection.set_trace_callback(statements.append)
                try:
                    rows = tuple(
                        connection.execute(
                            "SELECT * FROM evidence_item_revision "
                            "WHERE evidence_id = 'evidence.audit-head' "
                            "ORDER BY revision"
                        )
                    )
                    origins = tuple(
                        store_module._require_historical_evidence_revision_origin(
                            connection,
                            project_id=self.fixture.store.project_id,
                            row=row,
                        )
                        for row in rows
                    )
                finally:
                    connection.set_trace_callback(None)
                    store_module._ACTIVE_AUDIT_JOURNAL_INDEX.reset(token)
            self.assertEqual(build_index.call_count, 1)
            self.assertEqual(len(origins), 3)
            self.assertEqual(len(set(origins)), 3)
            self.assertFalse(
                any(
                    "JSON_EACH(J.AUXILIARY_WRITES_JSON)" in statement.upper()
                    for statement in statements
                ),
                statements,
            )

    def test_full_audit_rejects_unrelated_evidence_head_commit_corruption(self):
        self._advance_mutable_heads(1)
        self.fixture.commit = 12
        self.fixture.append_branch()
        with closing(sqlite3.connect(self.fixture.paths.database)) as connection:
            connection.execute("UPDATE evidence_item_head SET project_commit_no = 1 WHERE evidence_id = 'evidence.audit-head'")
            connection.commit()
        reopened = WorkspaceStore.open(self.fixture.paths)
        with self.assertRaisesRegex(WorkspaceIntegrityError, "Evidence current head is not journal-derived"):
            reopened.verify_integrity()

    def test_recovery_index_retains_historical_evidence_and_annotation_heads(self):
        digests = self._advance_mutable_heads(1)
        with self.fixture.store._connection(read_only=True) as connection:
            cut = int(connection.execute("SELECT current_project_commit FROM workspace_metadata").fetchone()[0])
        self._advance_mutable_heads(2, *digests)
        with self.fixture.store._connection(read_only=True) as connection:
            connection.execute("BEGIN")
            index = store_module._validated_journal_index(connection, project_id="project.rh")
            facts = store_module._direct_recovery_facts_from_connection(
                connection, project_id="project.rh", cut_project_commit=None, journal_index=index,
            )
            evidence_history = next(
                item for item in facts["head_history"]
                if item["kind"] == "evidence" and item["identity"] == "evidence.audit-head"
            )
            evidence_at_cut = next(
                item for item in reversed(evidence_history["revisions"])
                if int(item["project_commit"]) <= cut
            )
            self.assertEqual(int(evidence_at_cut["revision"]), 1)
            self.assertEqual(evidence_history["revisions"][-1]["revision"], 2)

            annotation_key = (
                AuxiliaryTable.CAPTURE_SCOPE_ANNOTATION_HEAD.value,
                store_module.canonical_json_bytes(
                    {"annotation_id": "annotation.audit-head"}
                ),
            )
            projections = tuple(
                (
                    int(journal.row["project_commit_no"]),
                    store_module._annotation_head_at_journal(
                        connection,
                        journal_index=index,
                        journal=journal,
                        annotation_id="annotation.audit-head",
                    ),
                )
                for journal, _reference in index.auxiliary_origins[annotation_key]
            )
            annotation_at_cut = next(
                projection
                for project_commit, projection in reversed(projections)
                if project_commit <= cut
            )
            self.assertEqual(int(annotation_at_cut["revision"]), 1)
            self.assertEqual(str(annotation_at_cut["payload_digest"]), digests[1])
            self.assertEqual(int(projections[-1][1]["revision"]), 2)

    def _forge_branch_bytes_and_all_uncommitted_checksums(self, branch_id):
        with closing(sqlite3.connect(self.fixture.paths.database)) as connection:
            connection.row_factory = sqlite3.Row
            row = dict(connection.execute(
                "SELECT * FROM branch_revision WHERE object_id = ? AND revision = 1", (branch_id,),
            ).fetchone())
            document = store_module.loads_strict_json_object(row["payload_json"])
            document["meaning"] = "Coordinated payload forgery; all row-local checksums also rewritten."
            payload = store_module.canonical_payload(document)
            row.update(payload_json=payload.text, payload_digest=payload.sha256)
            row_digest = store_module._row_digest(
                "branch_revision", {column: row[column] for column in store_module._REVISION_DIGEST_COLUMNS},
            )
            connection.execute(
                "UPDATE branch_revision SET payload_json = ?, payload_digest = ?, row_digest = ? "
                "WHERE object_id = ? AND revision = 1",
                (payload.text, payload.sha256, row_digest, branch_id),
            )
            connection.execute("UPDATE branch_head SET payload_digest = ? WHERE object_id = ?", (payload.sha256, branch_id))
            connection.commit()

    def test_current_delta_rejects_coordinated_payload_and_checksum_forgery(self):
        self._forge_branch_bytes_and_all_uncommitted_checksums("branch.root6.000010")
        with self.assertRaises(WorkspaceIntegrityError):
            WorkspaceStore.open(self.fixture.paths)

    def test_old_delta_digest_binding_rejects_coordinated_checksum_forgery(self):
        reference = self.fixture.branches[0]
        self._forge_branch_bytes_and_all_uncommitted_checksums(
            reference.object_id.value
        )
        reopened = WorkspaceStore.open(self.fixture.paths)
        with self.assertRaisesRegex(WorkspaceIntegrityError, "journaled typed revision digest binding mismatch"):
            reopened.verify_integrity()
        with self.assertRaises(WorkspaceIntegrityError):
            reopened.get_revision(reference)
        with self.assertRaises(WorkspaceIntegrityError):
            reopened.get_head(reference.object_id)

    def test_full_audit_recomputes_old_root6_even_with_matching_result(self):
        with closing(sqlite3.connect(self.fixture.paths.database)) as connection:
            connection.row_factory = sqlite3.Row
            row = connection.execute("SELECT * FROM command_result WHERE project_commit_no = 4").fetchone()
            result = store_module.loads_strict_json_object(row["result_json"])
            result["root_digest"] = "0" * 64
            payload = store_module.canonical_payload(result)
            connection.execute("UPDATE project_commit SET root_digest = ? WHERE commit_no = 4", ("0" * 64,))
            connection.execute(
                "UPDATE command_result SET result_json = ?, result_digest = ? WHERE project_commit_no = 4",
                (payload.text, payload.sha256),
            )
            connection.commit()
        reopened = WorkspaceStore.open(self.fixture.paths)
        with self.assertRaisesRegex(WorkspaceIntegrityError, "root6 commitment"):
            reopened.verify_integrity()

    def test_full_audit_rejects_unjournaled_auxiliary_rows(self):
        with closing(sqlite3.connect(self.fixture.paths.database)) as connection:
            connection.execute(
                "INSERT INTO blob(sha256, byte_length, media_type, encoding, integrity_state, "
                "availability_state, logical_cas_path, first_verified_at, last_verified_at, "
                "quarantine_state, quarantine_reason) "
                "SELECT ?, byte_length, media_type, encoding, integrity_state, availability_state, "
                "?, first_verified_at, last_verified_at, quarantine_state, quarantine_reason "
                "FROM blob LIMIT 1",
                ("b" * 64, "cas/sha256/bb/" + "b" * 62),
            )
            connection.commit()
        reopened = WorkspaceStore.open(self.fixture.paths)
        with self.assertRaisesRegex(WorkspaceIntegrityError, "no retained journal origin"):
            reopened.verify_integrity()

    def _insert_security_scope(self, connection, number, *, evidence_id=None, blob_digest=None):
        # Focused persisted-selector fixture, intentionally outside the writer.
        # These rows are not claimed to be a valid complete Store history.
        evidence_id = evidence_id or f"security.evidence.{number}"
        blob_digest = blob_digest or f"{number + 1:064x}"
        directive_id = f"security.directive.{number}"
        reason = "Exact synthetic security disposition."
        connection.execute(
            "INSERT INTO deletion_directive VALUES (?, ?, ?, 'active', 'fixture')",
            (directive_id, store_module.canonical_payload({"authorization_sha256": "a" * 64}).text,
             store_module.canonical_payload({"reason": reason, "blob_sha256s": [blob_digest],
                "evidence_references": [[evidence_id, 1]]}).text),
        )
        connection.execute(
            "INSERT INTO evidence_tombstone VALUES (?, ?, 1, ?, ?, 'fixture')",
            (f"security.tombstone.{number}", evidence_id, directive_id,
             store_module.canonical_payload({"reason_sha256": store_module._digest(reason.encode())}).text),
        )
        connection.execute("INSERT INTO evidence_blob VALUES (?, 1, 0, 'primary', ?)", (evidence_id, blob_digest))

    def test_unrelated_security_history_does_not_expand_current_closure_selector(self):
        work = []
        prior = 0
        for count in (0, 64, 512):
            with closing(sqlite3.connect(self.fixture.paths.database)) as mutation:
                for number in range(prior, count):
                    self._insert_security_scope(mutation, number)
                mutation.commit()
            prior = count
            with self.fixture.store._connection(read_only=True) as connection:
                # Equalize fixed SQLite schema/statement initialization before
                # measuring the same selected dependency set at every size.
                store_module._selected_closure_security_tombstones(
                    connection, blob_sha256s=(self.fixture.blob.sha256,),
                    evidence_references=(("evidence.1", 1),),
                )
                steps = 0
                def progress():
                    nonlocal steps
                    steps += 1
                    return 0
                connection.set_progress_handler(progress, 1)
                with patch.object(store_module, "_validated_journal_index", side_effect=AssertionError("unrelated security origin scan")):
                    records = store_module._closure_security_records(
                        connection, project_id="project.rh", root_digest_version=6,
                        blob_sha256s=(self.fixture.blob.sha256,), evidence_references=(("evidence.1", 1),),
                        journal_index=None, auxiliary_specs=store_module._ROOT6_AUXILIARY_SPECS,
                    )
                connection.set_progress_handler(None, 0)
                self.assertEqual(records, ((), ()))
                work.append(steps)
        self.assertLess(max(work) - min(work), 80, work)
        print("Security selector VM steps at 0/64/512 unrelated scopes:", work)
        with self.fixture.store._connection(read_only=True) as connection:
            plan = " ".join(str(row[3]) for row in connection.execute(
                "EXPLAIN QUERY PLAN SELECT t.* FROM evidence_blob AS b JOIN evidence_tombstone AS t "
                "ON t.evidence_id = b.evidence_id AND t.evidence_revision = b.evidence_revision WHERE b.blob_sha256 = ?",
                (self.fixture.blob.sha256,),
            ))
            self.assertIn("evidence_blob_shared_blob", plan)
            self.assertIn("evidence_tombstone_evidence", plan)

    def test_one_security_scope_map_preserves_matching_and_shared_blob_rejections(self):
        cas = EvidenceCAS(self.fixture.paths)
        manifest = valid_closure(cas, self.fixture.blob)
        with closing(sqlite3.connect(self.fixture.paths.database)) as mutation:
            self._insert_security_scope(
                mutation,
                0,
                blob_digest=self.fixture.blob.sha256,
            )
            self._insert_security_scope(mutation, 1, evidence_id="evidence.1")
            mutation.commit()
        with self.fixture.store._connection(read_only=True) as connection:
            with patch.object(store_module, "_closure_security_scope_index", wraps=store_module._closure_security_scope_index) as build:
                index = store_module._validated_journal_index(connection, project_id="project.rh")
                for _ in range(5):
                    directives, tombstones = store_module._closure_security_records(
                        connection, project_id="project.rh", root_digest_version=6,
                        blob_sha256s=(self.fixture.blob.sha256,), evidence_references=(("evidence.1", 1),),
                        journal_index=index, auxiliary_specs=store_module._ROOT6_AUXILIARY_SPECS,
                    )
                self.assertEqual(build.call_count, 1)
            self.assertEqual({item.directive_id for item in directives}, {"security.directive.0", "security.directive.1"})
            for selected_directives, selected_tombstones, error in (
                ((index.security_scopes.directives["security.directive.0"],), (), "security-deleted blob"),
                ((index.security_scopes.directives["security.directive.1"],), tombstones, "tombstoned Evidence"),
            ):
                with self.assertRaisesRegex(EvidenceStoreError, error):
                    verify_closure_manifest(manifest, blobs={self.fixture.blob.sha256: self.fixture.blob}, cas=cas,
                        deletion_directives=selected_directives, tombstones=selected_tombstones)
            selected = store_module._selected_closure_security_tombstones(
                connection, blob_sha256s=(self.fixture.blob.sha256,), evidence_references=(("evidence.1", 1),),
            )
            self.assertEqual({row["tombstone_id"] for row in selected}, {"security.tombstone.0", "security.tombstone.1"})
            # Exact routine access cannot treat an unowned selector hit as
            # authority.  Authenticated nonmembership is the current answer;
            # the complete audit remains responsible for rejecting the forged
            # durable rows that were never admitted to the projection.
            self.assertEqual(
                store_module._closure_security_records(
                    connection, project_id="project.rh", root_digest_version=6,
                    blob_sha256s=(self.fixture.blob.sha256,), evidence_references=(),
                    journal_index=None, auxiliary_specs=store_module._ROOT6_AUXILIARY_SPECS,
                ),
                ((), ()),
            )
        with self.assertRaises(WorkspaceIntegrityError):
            self.fixture.store.verify_integrity()

    def test_security_scope_parser_rejects_noncanonical_duplicate_and_malformed_state(self):
        with closing(sqlite3.connect(self.fixture.paths.database)) as connection:
            connection.row_factory = sqlite3.Row
            self._insert_security_scope(connection, 0)
            valid_scope = store_module.loads_strict_json_object(
                str(
                    connection.execute(
                        "SELECT exact_scope_json FROM deletion_directive "
                        "WHERE directive_id = 'security.directive.0'"
                    ).fetchone()[0]
                )
            )
            scenarios = (
                (
                    "duplicate-blob",
                    "UPDATE deletion_directive SET exact_scope_json = ? "
                    "WHERE directive_id = 'security.directive.0'",
                    (
                        store_module.canonical_payload(
                            {
                                **valid_scope,
                                "blob_sha256s": [
                                    valid_scope["blob_sha256s"][0],
                                    valid_scope["blob_sha256s"][0],
                                ],
                            }
                        ).text,
                    ),
                ),
                (
                    "malformed-evidence-reference",
                    "UPDATE deletion_directive SET exact_scope_json = ? "
                    "WHERE directive_id = 'security.directive.0'",
                    (
                        store_module.canonical_payload(
                            {
                                **valid_scope,
                                "evidence_references": [["security.evidence.0"]],
                            }
                        ).text,
                    ),
                ),
                (
                    "noncanonical-directive-json",
                    "UPDATE deletion_directive SET exact_scope_json = ? "
                    "WHERE directive_id = 'security.directive.0'",
                    (
                        '{"reason": "Exact synthetic security disposition.", '
                        '"blob_sha256s": ["' + valid_scope["blob_sha256s"][0]
                        + '"], "evidence_references": [["security.evidence.0", 1]]}',
                    ),
                ),
                (
                    "malformed-tombstone-reason",
                    "UPDATE evidence_tombstone SET reason_json = ? "
                    "WHERE tombstone_id = 'security.tombstone.0'",
                    (
                        store_module.canonical_payload(
                            {"reason_sha256": "a" * 64, "extra": True}
                        ).text,
                    ),
                ),
            )
            for name, sql, parameters in scenarios:
                with self.subTest(corruption=name):
                    connection.execute("SAVEPOINT malformed_security")
                    connection.execute(sql, parameters)
                    with self.assertRaisesRegex(
                        WorkspaceIntegrityError,
                        "not an exact typed record",
                    ):
                        store_module._closure_security_scope_index(connection)
                    connection.execute("ROLLBACK TO malformed_security")
                    connection.execute("RELEASE malformed_security")



    def test_native_head_cannot_forge_a_retained_genesis_origin(self):
        reference = self.fixture.branches[0]
        with closing(sqlite3.connect(self.fixture.paths.database)) as connection:
            connection.execute("UPDATE branch_head SET project_commit_no=0 WHERE object_id=?",
                               (reference.object_id.value,))
            connection.commit()
        with self.assertRaises(WorkspaceIntegrityError):
            self.fixture.store.get_head(reference.object_id)


if __name__ == "__main__":
    unittest.main()
