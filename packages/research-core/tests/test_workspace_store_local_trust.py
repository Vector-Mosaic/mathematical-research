from __future__ import annotations

import sqlite3
import json
import sys
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch


PACKAGE_ROOT = Path(__file__).resolve().parents[1]
TEST_ROOT = Path(__file__).resolve().parent
for candidate in (PACKAGE_ROOT, TEST_ROOT):
    if str(candidate) not in sys.path:
        sys.path.insert(0, str(candidate))

from research_core.workspace_schema import IdentityKind, RevisionRef, TypedWorkspaceId, canonical_payload  # noqa: E402
from research_core.workspace_store import (  # noqa: E402
    WorkspaceIntegrityError, WorkspaceStore, canonical_raw_capture_id,
    _REVISION_DIGEST_COLUMNS, _row_digest,
)
from research_core.evidence_store import EvidenceCAS, EvidenceStoreError  # noqa: E402
import test_workspace_store_successor_writes as successor_fixtures  # noqa: E402
import test_candidate_a1_triage_store as candidate_fixtures  # noqa: E402
import test_workspace_store_root6_asymptotics as asymptotic_fixtures  # noqa: E402
import research_core.workspace_store as store_module  # noqa: E402


class WorkspaceStoreLocalTrustTests(unittest.TestCase):
    def setUp(self) -> None:
        self.fixture = successor_fixtures.SuccessorWorkspaceWriterTests(methodName="runTest")
        self.addCleanup(self.fixture.doCleanups)
        self.fixture.setUp()
        self.store = self.fixture.store

    def _tamper(self, sql: str, parameters: tuple[object, ...] = ()) -> None:
        with sqlite3.connect(self.store.paths.database) as connection:
            connection.execute(sql, parameters)

    def _forge_context_checksums(self, identity: str, revision: int, *, current: bool) -> None:
        with sqlite3.connect(self.store.paths.database) as connection:
            connection.row_factory = sqlite3.Row
            row = connection.execute(
                "SELECT * FROM context_revision WHERE object_id = ? AND revision = ?",
                (identity, revision),
            ).fetchone()
            values = {column: row[column] for column in _REVISION_DIGEST_COLUMNS}
            document = json.loads(values["payload_json"])
            document["text"] = "forged-but-self-consistent"
            payload = canonical_payload(document)
            values.update(payload_json=payload.text, payload_digest=payload.sha256)
            connection.execute(
                "UPDATE context_revision SET payload_json = ?, payload_digest = ?, row_digest = ? "
                "WHERE object_id = ? AND revision = ?",
                (payload.text, payload.sha256, _row_digest("context_revision", values), identity, revision),
            )
            if current:
                connection.execute(
                    "UPDATE context_head SET payload_digest = ? WHERE object_id = ?",
                    (payload.sha256, identity),
                )

    def test_head_read_binds_untouched_old_owner_row_digest(self) -> None:
        self._tamper(
            "UPDATE mission_revision SET created_actor = ? WHERE object_id = ?",
            ("changed-outside-payload", self.fixture.mission_id),
        )
        reopened = WorkspaceStore.open(self.store.paths)
        with self.assertRaisesRegex(WorkspaceIntegrityError, "revision"):
            reopened.get_head(TypedWorkspaceId(IdentityKind.MISSION, self.fixture.mission_id))

    def test_head_read_binds_exact_owner_commit(self) -> None:
        current_commit = int(self.store.read_metadata()["current_project_commit"])
        self._tamper(
            "UPDATE mission_head SET project_commit_no = ? WHERE object_id = ?",
            (current_commit, self.fixture.mission_id),
        )
        with self.assertRaisesRegex(
            WorkspaceIntegrityError, "ownership|journaled row binding"
        ):
            self.store.get_head(TypedWorkspaceId(IdentityKind.MISSION, self.fixture.mission_id))

    def test_head_read_rejects_dangling_projection(self) -> None:
        self._tamper(
            "UPDATE mission_head SET revision = 99 WHERE object_id = ?",
            (self.fixture.mission_id,),
        )
        with self.assertRaisesRegex(WorkspaceIntegrityError, "absent"):
            self.store.get_head(TypedWorkspaceId(IdentityKind.MISSION, self.fixture.mission_id))

    def test_exact_historical_read_checks_the_requested_full_row(self) -> None:
        owner = TypedWorkspaceId(IdentityKind.CONTEXT, "context.local-trust")
        first = self.fixture._commit_context(
            context_id=owner.value, payload={"text": "first"}, command_id="local.first",
            expected_revision=None, expected_digest=None,
        )
        stored = self.store.get_head(owner)
        assert stored is not None
        self.fixture._commit_context(
            context_id=owner.value, payload={"text": "second"}, command_id="local.second",
            expected_revision=1, expected_digest=stored.payload_digest,
        )
        self.assertGreater(first.project_commit, 0)
        self._forge_context_checksums(owner.value, 1, current=False)
        self.assertEqual(self.store.get_head(owner).reference.revision, 2)
        with self.assertRaisesRegex(WorkspaceIntegrityError, "revision"):
            self.store.get_revision(RevisionRef(owner, 1))

    def test_old_current_head_rejects_coordinated_row_and_projection_forgery(self) -> None:
        owner = TypedWorkspaceId(IdentityKind.CONTEXT, "context.old-current")
        self.fixture._commit_context(
            context_id=owner.value, payload={"text": "original"}, command_id="forged.original",
            expected_revision=None, expected_digest=None,
        )
        self.fixture._commit_context(
            context_id="context.newer", payload={"text": "unrelated"}, command_id="forged.newer",
            expected_revision=None, expected_digest=None,
        )
        self._forge_context_checksums(owner.value, 1, current=True)
        reopened = WorkspaceStore.open(self.store.paths)
        with self.assertRaisesRegex(WorkspaceIntegrityError, "journaled row binding"):
            reopened.get_head(owner)
        with self.assertRaisesRegex(WorkspaceIntegrityError, "journaled row binding"):
            reopened.get_revision(RevisionRef(owner, 1))

    def test_write_precondition_rejects_unaccessed_target_projection(self) -> None:
        owner = TypedWorkspaceId(IdentityKind.CONTEXT, "context.target")
        self.fixture._commit_context(
            context_id=owner.value, payload={"text": "first"}, command_id="target.first",
            expected_revision=None, expected_digest=None,
        )
        head = self.store.get_head(owner)
        assert head is not None
        self.fixture._commit_context(
            context_id="context.other", payload={"text": "unrelated"},
            command_id="target.unrelated", expected_revision=None, expected_digest=None,
        )
        self._tamper(
            "UPDATE context_head SET payload_digest = ? WHERE object_id = ?",
            ("0" * 64, owner.value),
        )
        with self.assertRaises(WorkspaceIntegrityError):
            self.fixture._commit_context(
                context_id=owner.value, payload={"text": "second"}, command_id="target.second",
                expected_revision=1, expected_digest=head.payload_digest,
            )

    def test_historical_capture_artifact_read_binds_origin_beyond_cas_bytes(self) -> None:
        capture_id = self.fixture._commit_raw_capture(suffix="local-origin")
        self.fixture._commit_context(
            context_id="context.after-capture", payload={"text": "later"},
            command_id="capture.unrelated", expected_revision=None, expected_digest=None,
        )
        self._tamper(
            "UPDATE raw_capture_artifact SET logical_name = ? WHERE capture_id = ?",
            ("changed-while-cas-is-valid.txt", capture_id),
        )
        reopened = WorkspaceStore.open(self.store.paths)
        with self.assertRaisesRegex(WorkspaceIntegrityError, "exact row"):
            reopened.read_raw_capture(capture_id)
        with self.assertRaisesRegex(WorkspaceIntegrityError, "exact row"):
            reopened.read_raw_capture_artifact_bytes(
                cas=EvidenceCAS(reopened.paths), mission_id=self.fixture.mission_id,
                capture_id=capture_id, artifact_ordinal=0,
            )

    def test_public_raw_capture_read_begins_before_current_boundary(self) -> None:
        capture_id = self.fixture._commit_raw_capture(suffix="held-boundary")
        current_boundary = WorkspaceStore._verify_current_boundary
        boundary_calls = 0

        def verify_held_boundary(store, connection, *args, **kwargs):
            nonlocal boundary_calls
            self.assertTrue(connection.in_transaction)
            boundary_calls += 1
            return current_boundary(store, connection, *args, **kwargs)

        with patch.object(
            WorkspaceStore,
            "_verify_current_boundary",
            new=verify_held_boundary,
        ):
            captures = self.store.list_mission_raw_captures(self.fixture.mission_id)

        self.assertEqual(
            tuple(item["record"]["capture_id"] for item in captures),
            (capture_id,),
        )
        self.assertEqual(boundary_calls, 1)

    def test_capture_origin_index_selects_multiple_blobs_and_artifacts(self) -> None:
        cas = EvidenceCAS(self.store.paths)
        records = tuple(
            replace(
                cas.ingest_bytes(
                    f"indexed-capture-{ordinal}".encode(), original_name=f"item{ordinal}.txt",
                    media_type="text/plain", encoding="utf-8",
                ).record,
                availability_state="verified_available",
            )
            for ordinal in range(3)
        )
        capture_id = canonical_raw_capture_id(
            project_id=self.store.project_id, capture_kind="output", observation_id="index.capture",
        )
        record = {
            "capture_id": capture_id, "mission_id": self.fixture.mission_id,
            "executive_epoch_id": self.fixture.executive_epoch_id, "capture_kind": "output",
            "observation_id": "index.capture", "assignment_id": "index.assignment",
            "provenance": {"kind": "index-test"}, "completion": {"lifecycle": "completed"},
        }
        with self.assertRaisesRegex(ValueError, "at least one exact artifact"):
            self.store.commit_raw_capture(
                record=record, artifacts=(), blobs=(), lease=self.fixture.lease,
                command_id="index.empty", actor="successor-writer-test",
            )
        outcome = self.store.commit_raw_capture(
            record=record,
            artifacts=tuple({
                "ordinal": ordinal, "role": "result", "logical_name": f"item{ordinal}.txt",
                "blob_sha256": blob.sha256,
            } for ordinal, blob in enumerate(records)),
            blobs=records, lease=self.fixture.lease, command_id="index.multiple",
            actor="successor-writer-test",
        )
        with self.store._connection(read_only=True) as connection:
            sql = (
                "SELECT project_commit_no FROM transition_journal "
                "WHERE command_kind = 'commit_raw_capture' "
                "AND json_extract(auxiliary_writes_json, '$[#-1].primary_key.capture_id') = ?"
            )
            plan = " ".join(str(row[3]) for row in connection.execute("EXPLAIN QUERY PLAN " + sql, (capture_id,)))
            self.assertIn("USING INDEX transition_journal_capture_id", plan)
            self.assertEqual([int(row[0]) for row in connection.execute(sql, (capture_id,))], [outcome.project_commit])
        self.assertEqual(len(self.store.read_raw_capture(capture_id)["artifacts"]), 3)
        for ordinal in range(3):
            _descriptor, raw = self.store.read_raw_capture_artifact_bytes(
                cas=cas, mission_id=self.fixture.mission_id, capture_id=capture_id,
                artifact_ordinal=ordinal,
            )
            self.assertEqual(raw, f"indexed-capture-{ordinal}".encode())


class WorkspaceStoreA1LocalScalingTests(unittest.TestCase):
    def _a1_fixture(self):
        fixture = candidate_fixtures.CandidateA1TriageStoreTests(methodName="runTest")
        self.addCleanup(fixture.doCleanups)
        fixture.setUp()
        return fixture

    @staticmethod
    def _a1_binding(fixture, candidate):
        return fixture.store.read_candidate_a1_binding(
            candidate_id=str(candidate["candidate_id"]),
            candidate_revision=int(candidate["revision"]),
            candidate_digest=str(candidate["payload_digest"]),
        )

    def test_old_triage_and_admission_reader_reject_coordinated_evidence_forgery(self) -> None:
        fixture = self._a1_fixture()
        support = fixture._commit_supporting_evidence("trust")
        candidate = fixture._commit_full_rh_candidate(
            "candidate.triage-forgery",
            supporting_refs=(support,),
        )
        record = candidate_fixtures.prepare_candidate_a1_triage(
            candidate_ref=fixture._candidate_ref(candidate),
            disposition=candidate_fixtures.CANDIDATE_A1_TRIAGE_ADMISSION_READY,
            reviewer=fixture._reviewer("trust"),
            review_finding="Independent reconstruction leaves no material objection to this exact claim.",
            cited_basis=(support,),
            no_remaining_material_objection=True,
            limitations=("Admission authority remains separate",),
        )
        fixture.store.commit_candidate_a1_triage_revision(
            executive_epoch_id=fixture.executive_epoch_id,
            record=record,
            source=fixture._triage_source(record=record, suffix="trust"),
            lease=fixture.lease,
            command_id="candidate.triage-forgery.triage",
            actor="worker.independent-reviewer.trust",
            expected_canonical_authority_digest=fixture.authority_digest,
        )
        fixture._commit_full_rh_candidate("candidate.after-triage-forgery")
        evidence_id = record.evidence.evidence_id
        with sqlite3.connect(fixture.store.paths.database) as connection:
            connection.row_factory = sqlite3.Row
            row = connection.execute(
                "SELECT * FROM evidence_item_revision WHERE evidence_id = ? AND revision = 1",
                (evidence_id,),
            ).fetchone()
            spec = store_module._AUXILIARY_SPECS[store_module.AuxiliaryTable.EVIDENCE_ITEM_REVISION]
            values = {
                column: json.loads(row[column]) if column in spec.json_columns else row[column]
                for column in spec.columns
            }
            values["subject_json"]["review_finding"] = "Forged review finding with coordinated valid checksums."
            forged = canonical_payload(store_module._evidence_revision_payload_from_values(values, ()))
            connection.execute(
                "UPDATE evidence_item_revision SET subject_json = ?, payload_digest = ? "
                "WHERE evidence_id = ? AND revision = 1",
                (canonical_payload(values["subject_json"]).text, forged.sha256, evidence_id),
            )
            connection.execute(
                "UPDATE evidence_item_head SET payload_digest = ? WHERE evidence_id = ?",
                (forged.sha256, evidence_id),
            )
        reopened = WorkspaceStore.open(fixture.store.paths)
        with self.assertRaisesRegex(WorkspaceIntegrityError, "ownership"):
            reopened.read_candidate_a1_triage_revision(
                mission_id=record.candidate_ref.mission_id,
                candidate_id=record.candidate_ref.candidate_id,
                candidate_revision=record.candidate_ref.revision,
                candidate_digest=record.candidate_ref.digest_sha256,
            )
        with reopened._connection(read_only=True) as connection:
            # The same exact reader consumes Admission Cases, Reviews, Decisions,
            # and their immutable triage eligibility input.
            with self.assertRaisesRegex(
                WorkspaceIntegrityError, "ownership|journaled row binding"
            ):
                store_module._admission_evidence_from_connection(
                    connection, evidence_id=evidence_id, subtype=record.evidence.subtype,
                )

    def test_public_native_closure_and_handle_use_exact_origin(self) -> None:
        fixture = self._a1_fixture()
        candidate = fixture._commit_full_rh_candidate("candidate.public-closure")
        binding = self._a1_binding(fixture, candidate)
        cas = EvidenceCAS(fixture.store.paths)
        with (
            patch.object(store_module, "_validated_journal_index", side_effect=AssertionError("public native closure built full origin index")),
            patch.object(store_module, "_validated_transition_journal_chain", side_effect=AssertionError("public native closure scanned journal")),
        ):
            bundle = fixture.store.load_verified_closure_bundle(cas=cas, closure_id=binding.closure_id)
            handle = fixture.store.issue_evidence_read_handle(
                cas=cas, closure_id=binding.closure_id, evidence_id=binding.evidence_id,
                evidence_revision=1, role=store_module._PURPORTED_COMPLETE_ARTIFACT_ROLE,
                authorization={"purpose": "exact public native read"},
            )
        self.assertEqual(bundle.manifest.manifest_sha256, binding.closure_digest)
        self.assertEqual(handle.blob_sha256, binding.artifact_digest)

    def test_verified_evidence_selects_and_validates_only_its_exact_closure(self) -> None:
        fixture = self._a1_fixture()
        target = self._a1_binding(fixture, fixture._commit_full_rh_candidate("candidate.closure-target"))
        unrelated = self._a1_binding(fixture, fixture._commit_full_rh_candidate("candidate.closure-unrelated"))
        fixture._commit_full_rh_candidate("candidate.closure-latest")
        with sqlite3.connect(fixture.store.paths.database) as connection:
            document = json.loads(connection.execute(
                "SELECT contract_json FROM closure_manifest WHERE closure_id = ?", (unrelated.closure_id,),
            ).fetchone()[0])
            document["manifest_sha256"] = "0" * 64
            connection.execute(
                "UPDATE closure_manifest SET contract_json = ? WHERE closure_id = ?",
                (canonical_payload(document).text, unrelated.closure_id),
            )
        cas = EvidenceCAS(fixture.store.paths)
        with (
            patch.object(store_module, "_validated_journal_index", side_effect=AssertionError("exact Evidence read built full origin index")),
            patch.object(store_module, "_validated_transition_journal_chain", side_effect=AssertionError("exact Evidence read scanned journal")),
        ):
            verified = fixture.store.read_verified_evidence_revision(cas=cas, evidence_id=target.evidence_id)
            self.assertEqual(verified.closure.manifest.closure_id, target.closure_id)
            with self.assertRaises((WorkspaceIntegrityError, EvidenceStoreError)):
                fixture.store.read_verified_evidence_revision(cas=cas, evidence_id=unrelated.evidence_id)
        with fixture.store._connection(read_only=True) as connection:
            query = (
                "SELECT closure_id, contract_json FROM closure_manifest "
                "WHERE json_extract(contract_json, '$.root.relation') = 'evidence' "
                "AND json_extract(contract_json, '$.root.target_kind') = 'evidence' "
                "AND json_extract(contract_json, '$.root.target_id') = ? "
                "AND json_extract(contract_json, '$.root.target_revision') = ? ORDER BY closure_id"
            )
            plan = " ".join(str(row[3]) for row in connection.execute(
                "EXPLAIN QUERY PLAN " + query, (target.evidence_id, 1),
            ))
            self.assertIn("USING INDEX closure_manifest_evidence_root", plan)
            self.assertEqual(tuple(connection.execute(query, ("native-material-with-no-closure", 1))), ())
            for table, identity_column, index in (
                ("provenance_event", "provenance_id", "provenance_event_evidence_revision"),
                ("independence_disclosure", "disclosure_id", "independence_disclosure_evidence_revision"),
            ):
                selected_plan = " ".join(str(row[3]) for row in connection.execute(
                    f"EXPLAIN QUERY PLAN SELECT * FROM {table} WHERE evidence_id = ? "
                    f"AND evidence_revision = ? ORDER BY {identity_column}", (target.evidence_id, 1),
                ))
                self.assertIn(f"USING INDEX {index}", selected_plan)

    def test_verified_closure_rejects_mutated_or_extra_custody_rows(self) -> None:
        scenarios = (
            (
                "provenance-body",
                "UPDATE provenance_event SET origin_json = ? "
                "WHERE evidence_id = ? AND evidence_revision = 1",
                (canonical_payload({"tampered": True}).text,),
            ),
            (
                "disclosure-body",
                "UPDATE independence_disclosure SET model_json = ? "
                "WHERE evidence_id = ? AND evidence_revision = 1",
                (canonical_payload({"tampered": True}).text,),
            ),
            (
                "extra-provenance-row",
                "INSERT INTO provenance_event("
                "provenance_id, evidence_id, evidence_revision, origin_json, "
                "activity_json, agent_json, tool_json, input_json, "
                "transformation_json, custody_json, created_at) "
                "SELECT provenance_id || '.extra', evidence_id, "
                "evidence_revision, origin_json, activity_json, agent_json, "
                "tool_json, input_json, transformation_json, custody_json, "
                "created_at FROM provenance_event WHERE evidence_id = ? "
                "AND evidence_revision = 1",
                (),
            ),
        )
        for name, sql, prefix in scenarios:
            with self.subTest(corruption=name):
                fixture = self._a1_fixture()
                target = self._a1_binding(
                    fixture,
                    fixture._commit_full_rh_candidate(
                        f"candidate.closure-custody-{name}"
                    ),
                )
                with sqlite3.connect(fixture.store.paths.database) as connection:
                    connection.execute(sql, (*prefix, target.evidence_id))
                cas = EvidenceCAS(fixture.store.paths)
                with self.assertRaises((WorkspaceIntegrityError, EvidenceStoreError)):
                    with fixture.store._connection(read_only=True) as connection:
                        connection.execute("BEGIN")
                        fixture.store._load_verified_closure_bundle_from_connection(
                            connection,
                            cas=cas,
                            closure_id=target.closure_id,
                            metadata=fixture.store._metadata(connection),
                        )
                with self.assertRaises((WorkspaceIntegrityError, EvidenceStoreError)):
                    fixture.store.read_verified_evidence_revision(
                        cas=cas,
                        evidence_id=target.evidence_id,
                    )

    def test_exact_candidate_a1_read_and_write_scale_with_current_route_tree(self) -> None:
        fixture = self._a1_fixture()
        with (
            patch.object(store_module, "_validated_journal_index", side_effect=AssertionError("routine A1 built full journal index")),
            patch.object(store_module, "_validated_transition_journal_chain", side_effect=AssertionError("routine A1 scanned full journal")),
        ):
            target = fixture._commit_full_rh_candidate("candidate.target")

            def read_target():
                return fixture.store.read_candidate_a1_binding(
                    candidate_id=str(target["candidate_id"]),
                    candidate_revision=int(target["revision"]),
                    candidate_digest=str(target["payload_digest"]),
                )

            _small_binding, small_read = asymptotic_fixtures._measure(read_target, routine=True)
            _small_candidate, small_write = asymptotic_fixtures._measure(
                lambda: fixture._commit_full_rh_candidate("candidate.probe.small"), routine=True,
            )
            for ordinal in range(24):
                fixture._commit_full_rh_candidate(f"candidate.irrelevant.{ordinal:03d}")
            _large_binding, large_read = asymptotic_fixtures._measure(read_target, routine=True)
            _large_candidate, large_write = asymptotic_fixtures._measure(
                lambda: fixture._commit_full_rh_candidate("candidate.probe.large"), routine=True,
            )
        # These 24 routes are current OPEN restrictions, not retained-only
        # history. Exact membership and updates may therefore grow with the
        # logarithmic AVL path, but must not enumerate the route population.
        self.assertLessEqual(len(large_read.statements), len(small_read.statements) + 32)
        self.assertLessEqual(len(large_write.statements), len(small_write.statements) + 64)
        self.assertLessEqual(large_read.vm_steps, small_read.vm_steps + 1000)
        self.assertLessEqual(large_write.vm_steps, small_write.vm_steps + 4000)
        for small, large in ((small_read, large_read), (small_write, large_write)):
            self.assertFalse(any(
                "FROM evidence_item_head h JOIN evidence_item_revision r" in statement
                and "WHERE r.subtype = 'purported_complete_route' ORDER BY" in statement
                for statement in large.statements
            ))
            self.assertFalse(any(
                "SELECT DISTINCT executive_epoch_id FROM executive_epoch_event" in statement
                or (
                    "WHERE command_kind = 'commit_candidate_revision'" in statement
                    and "changed_heads_json =" not in statement
                )
                for statement in large.statements
            ))
        with fixture.store._connection(read_only=True) as connection:
            evidence_plan = " ".join(str(row[3]) for row in connection.execute(
                "EXPLAIN QUERY PLAN SELECT h.evidence_id FROM evidence_item_revision r "
                "JOIN evidence_item_head h ON r.evidence_id = h.evidence_id AND r.revision = h.revision "
                "WHERE r.subtype = 'purported_complete_route' "
                "AND json_extract(r.subject_json, '$.candidate_id') = ? "
                "AND json_extract(r.subject_json, '$.artifact_digest') = ? ORDER BY h.evidence_id",
                (target["candidate_id"], target["payload_digest"]),
            ))
            hold_plan = " ".join(str(row[3]) for row in connection.execute(
                "EXPLAIN QUERY PLAN SELECT resolution_id, authorization_json, resolution_json "
                "FROM hold_resolution WHERE hold_id = ? ORDER BY resolution_id", ("exact-hold",),
            ))
            candidate_origin_plan = " ".join(
                str(row[3])
                for row in connection.execute(
                    "EXPLAIN QUERY PLAN SELECT project_commit_no FROM transition_journal "
                    "WHERE command_kind = 'commit_candidate_revision' "
                    "AND changed_heads_json = ? ORDER BY project_commit_no LIMIT 2",
                    (
                        canonical_payload(
                            [
                                f"candidate:{target['candidate_id']}@"
                                f"{target['revision']}"
                            ]
                        ).text,
                    ),
                )
            )
            epoch_boundary_plan = " ".join(
                str(row[3])
                for row in connection.execute(
                    "EXPLAIN QUERY PLAN SELECT executive_epoch_id, event_kind "
                    "FROM executive_epoch_event WHERE mission_id = ? "
                    "AND project_commit_no < ? ORDER BY project_commit_no DESC, "
                    "event_ordinal DESC LIMIT 1",
                    (fixture._candidate_ref(target).mission_id, 1000000),
                )
            )
        self.assertIn("USING INDEX evidence_revision_candidate_a1_artifact", evidence_plan)
        self.assertIn("USING INDEX hold_resolution_hold", hold_plan)
        self.assertIn("SEARCH", candidate_origin_plan)
        self.assertIn("transition_journal_candidate_revision", candidate_origin_plan)
        self.assertIn("USING COVERING INDEX executive_epoch_event_mission_boundary", epoch_boundary_plan)
        print("A1 bounded SQL work:", {
            "read_route_counts": [1, 26], "post_write_route_counts": [2, 27],
            "small_read": small_read.summary(), "large_read": large_read.summary(),
            "small_write": small_write.summary(), "large_write": large_write.summary(),
        })

    def test_exact_candidate_a1_read_ignores_retained_non_a1_history(self) -> None:
        fixture = self._a1_fixture()
        target = fixture._commit_full_rh_candidate("candidate.fixed-open-set")

        def read_target():
            return fixture.store.read_candidate_a1_binding(
                candidate_id=str(target["candidate_id"]),
                candidate_revision=int(target["revision"]),
                candidate_digest=str(target["payload_digest"]),
            )

        fixture._commit_supporting_evidence("history-before-small")
        with (
            patch.object(
                store_module,
                "_derive_current_dependency_entries",
                side_effect=AssertionError("routine A1 read rederived current state"),
            ),
            patch.object(
                store_module,
                "_validated_journal_index",
                side_effect=AssertionError("routine A1 read built full journal index"),
            ),
            patch.object(
                store_module,
                "_validated_transition_journal_chain",
                side_effect=AssertionError("routine A1 read scanned full journal"),
            ),
        ):
            small_binding, small = asymptotic_fixtures._measure(
                read_target, routine=True
            )
            for ordinal in range(12):
                fixture._commit_supporting_evidence(
                    f"retained-history-{ordinal:03d}"
                )
            large_binding, large = asymptotic_fixtures._measure(
                read_target, routine=True
            )

        self.assertEqual(small_binding.candidate_id, str(target["candidate_id"]))
        self.assertEqual(large_binding.candidate_id, str(target["candidate_id"]))
        self.assertLessEqual(large.vm_steps, small.vm_steps + 1000)
        # These Evidence fixtures create current raw artifacts even though they
        # create no A1 route. Current-boundary validation reads only the coverage
        # root and its at-most-two immediate child summaries; it never follows
        # retained history or recursively walks the coverage tree.
        small_coverage_root_reads = sum(
            "FROM capture_artifact_coverage_node WHERE node_digest" in statement
            for statement in small.statements
        )
        large_coverage_root_reads = sum(
            "FROM capture_artifact_coverage_node WHERE node_digest" in statement
            for statement in large.statements
        )
        self.assertLessEqual(small_coverage_root_reads, 3)
        self.assertLessEqual(large_coverage_root_reads, 3)
        self.assertEqual(
            len(small.statements) - small_coverage_root_reads,
            len(large.statements) - large_coverage_root_reads,
        )
        for statement in large.statements:
            self.assertNotIn(
                "FROM candidate_revision ORDER BY object_id, revision",
                statement,
            )
            self.assertNotIn(
                "FROM hold h JOIN alert_event e",
                statement,
            )
        print("A1 retained-history-independent read:", {
            "small": small.summary(),
            "large": large.summary(),
        })

    def test_full_audit_scales_linearly_with_candidate_a1_history(self) -> None:
        fixture = self._a1_fixture()
        observations = []
        populated = 0
        for size in (4, 16, 32):
            while populated < size:
                fixture._commit_full_rh_candidate(
                    f"candidate.audit-scaling.{populated:03d}"
                )
                populated += 1
            with patch.object(
                store_module,
                "_validated_transition_journal_chain",
                wraps=store_module._validated_transition_journal_chain,
            ) as chain:
                report, work = asymptotic_fixtures._measure(
                    fixture.store.verify_integrity,
                    routine=False,
                )
            self.assertEqual(chain.call_count, 1)
            a2_full_enumerations = sum(
                "FROM hold h JOIN alert_event e" in statement
                and "e.severity = 'A2'" in statement
                and "h.lifecycle = 'open'" in statement
                for statement in work.statements
            )
            self.assertEqual(a2_full_enumerations, 1, work.statements)
            self.assertEqual(
                len(
                    fixture.store._list_open_mission_candidate_a1_bindings(
                        "mission.1"
                    )
                ),
                size,
            )
            observations.append(
                {
                    "candidate_a1_count": size,
                    "project_commit": report.current_project_commit,
                    "a2_full_enumerations": a2_full_enumerations,
                    "work": work.summary(),
                }
            )

        for metric in ("statements", "vm_steps_100_quantum"):
            first, middle, last = (
                item["work"][metric] for item in observations
            )
            early_slope = (middle - first) / (16 - 4)
            late_slope = (last - middle) / (32 - 16)
            self.assertGreater(early_slope, 0, (metric, observations))
            self.assertGreater(late_slope, 0, (metric, observations))
            self.assertLessEqual(
                late_slope,
                early_slope * 1.5,
                (metric, observations),
            )
        print("A1 full-audit scaling:", observations)


if __name__ == "__main__":
    unittest.main()
