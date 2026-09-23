"""Real Store/semantic-boundary D1 regressions; run in isolated RH staging.

Reuses the existing direct-Mission fixture lifecycle rather than a fake writer.
These deterministic checks are not model-bearing behavioral qualification.
"""
from __future__ import annotations

import json
import sqlite3
import unittest
from contextlib import closing
from dataclasses import replace
from unittest import mock

import test_mission_interface_direct as direct_fixture
from research_core import owner_content_index, workspace_store
from research_core.context_revision import prepare_scientific_context_update, read_context_revision
from research_core.mission_evidence import read_evidence_meaning
from research_core.mission_operation_contract import (
    CHECKPOINT, INTERPRET_MATERIAL, RECORD_CANDIDATE, RECORD_CONTEXT, RETRIEVE, SYNTHESIZE,
)
from research_core.research_model import deep_thaw
from research_core.workspace_schema import IdentityKind, RevisionRef, TypedWorkspaceId, verify_schema_contract
from research_core.workspace_store import CommandConflictError, StaleCommandError, WorkspaceIntegrityError
from research_core.workspace_store import _scientific_publication_result_from_receipt


class ScientificPublicationTests(unittest.TestCase):
    # Borrow only setup helpers, not the hundreds of unrelated inherited tests.
    setUpClass = classmethod(direct_fixture.DirectMissionInterfaceTests.setUpClass.__func__)
    setUp = direct_fixture.DirectMissionInterfaceTests.setUp
    _request = staticmethod(direct_fixture.DirectMissionInterfaceTests._request)
    _execute = direct_fixture.DirectMissionInterfaceTests._execute
    _authorize = direct_fixture.DirectMissionInterfaceTests._authorize
    _bind = direct_fixture.DirectMissionInterfaceTests._bind

    @staticmethod
    def _treatment(account="An open consumer question; no theorem is asserted.", sources=()):
        return {"question": "What does the qualified ingredient permit?", "account": account,
                "qualifications": ["Lower gaps do not imply an upper mismatch bound."],
                "sources": list(sources)}

    @staticmethod
    def _document(identity, treatments=None, exposed=()):
        return {"schema_version": 3, "kind": "first_class_context", "project_id": "project.rh",
                "mission_id": "mission.1", "context_id": identity,
                "purpose": "Mission-wide scientific understanding independent of Strategy.",
                "question": "Which parent questions and qualified interfaces remain?",
                "treatments": {} if treatments is None else treatments,
                "exposed_treatments": list(exposed), "known_omissions": [], "restricted_uses": [],
                "restrictions": ["No RH conclusion follows from this account."],
                "independence_treatment": {"treatment": "ordinary integrated judgment"}}

    def _reference(self, identity):
        value = read_context_revision(self.store, context_id=identity)
        return {"kind": "context", "identity": identity, "revision": value.revision,
                "payload_sha256": value.payload_digest}

    def _create(self, identity, document=None):
        return {"context_id": f"context:{identity}", "expected_head": None,
                "create": self._document(identity) if document is None else document, "patch": None}

    def _patch(self, identity, *, insert=None, replace=None, remove=(), add=(), drop=()):
        return {"context_id": f"context:{identity}", "expected_head": self._reference(identity), "create": None,
                "patch": {"insert": insert or {}, "replace": replace or {}, "remove": list(remove),
                          "exposure_add": list(add), "exposure_remove": list(drop), "metadata_replace": {}}}

    def _context(self, updates, *, required=()):
        response = self._execute(RECORD_CONTEXT, {"mode": "scientific", "updates": updates,
                                                 "exposure_required": list(required)}, executive_epoch_id=self.epoch)
        self.assertEqual(response["status"], "completed", response)
        return response["result"]

    def _open(self, *, bound=False):
        self.epoch = self._authorize()
        self._bind(self.epoch)
        self._context([self._create("science")])
        if bound:
            checkpoint = self._execute(CHECKPOINT, {}, executive_epoch_id=self.epoch)
            self.assertEqual(checkpoint["status"], "completed", checkpoint)
            mission = self.store.get_head(TypedWorkspaceId(IdentityKind.MISSION, "mission.1"))
            root = self._reference("science")
            self.interface.bind_scientific_context_from_owner(
                expected_mission_revision=mission.reference.revision,
                expected_mission_payload_sha256=mission.payload_digest,
                expected_canonical_authority_digest=self.store.read_metadata()["canonical_authority_digest"],
                context_id="science", expected_context_revision=root["revision"],
                expected_context_payload_sha256=root["payload_sha256"],
            )
            self.goal_thread_id = "goal-thread:scientific-publication-bound-successor"
            self.goal_workspace = self.goal_workspace.parent / "scientific-bound-successor"
            self.goal_workspace.mkdir()
            self.epoch = self._authorize()
            self._bind(self.epoch)

    def _interpretation(self, identity="ingredient", *, continuation=None):
        value = {"evidence_id": f"evidence:{identity}", "capture_scopes": [{
            "adopted_root_material": {"channel": "shell", "content": f"Exact bounded fixture output: {identity}."},
            "exact_scope": "The complete deterministic fixture output.", "coverage": "complete_artifact"}],
            "interpretation": "The finite diagnostic provides a stronger lower-gap ingredient.",
            "scope": "This finite fixture only.", "strength": "numerical_diagnostic",
            "limitations": ["It supplies no upper mismatch estimate."],
            "significance": "The consumer question remains open."}
        if continuation is not None:
            value.update(expected_head=None, scientific_continuation=continuation)
        return value

    def _continuation(self, *, deeper=False):
        source = {"reference": {"source": "this_evidence"}, "selection": None,
                  "roles": ["recognition"], "why": "This ingredient motivates the unresolved consumer question.",
                  "dependency": None}
        treatment = self._treatment(sources=[source])
        if deeper:
            pair = {"context_id": "consumer", "treatment_id": "a"}
            updates = [self._create("consumer", self._document("consumer", {"a": treatment})),
                       self._patch("science", add=[pair])]
        else:
            pair = {"context_id": "science", "treatment_id": "a"}
            updates = [self._patch("science", insert={"a": treatment})]
        return {"updates": updates, "exposure_required": [pair]}

    def test_fresh_schema12_root6_metadata_read_uses_authenticated_current_schema(self):
        metadata = self.store.read_metadata()
        self.assertEqual(metadata["schema_version"], 12)
        self.assertEqual(metadata["root_digest_version"], 6)
        with self.store._connection(read_only=True) as connection:
            schema = verify_schema_contract(connection, schema_version=12)
            body, persisted_root = direct_fixture.independent_root6_body_at_commit(
                connection,
                schema_contract={
                    "schema_version": schema.schema_version,
                    "migration_set_digest": schema.migration_digest,
                    "schema_object_digest": schema.schema_object_digest,
                },
                project_commit_no=metadata["current_project_commit"],
            )
        self.assertEqual(direct_fixture.independent_root6_digest(body), persisted_root)
        self.assertEqual(metadata["current_root_digest"], persisted_root)
        body["schema_contract"]["schema_version"] = 10
        self.assertNotEqual(direct_fixture.independent_root6_digest(body), persisted_root)
        self.assertEqual(self.store.read_metadata()["current_root_digest"], persisted_root)
        with self.store.direct_recovery_read_scope():
            cut = self.store.read_root_retrieval_cut()
        self.assertEqual(cut["route"], "native_root6_indexed")
        self.assertEqual(cut["project_commit"], metadata["current_project_commit"])
        self.assertEqual(cut["root_digest"], persisted_root)

    def test_internal_result_contract_failure_is_unavailable_without_reclassifying_invalid_request(self):
        self._open()
        request = {"mode": "scientific", "updates": [self._patch("science", insert={"a": self._treatment()})],
                   "exposure_required": []}
        publish = self.interface._publish_scientific_context
        published = {}

        def corrupt_result(*args, **kwargs):
            result = deep_thaw(publish(*args, **kwargs))
            published.update(result)
            result["schema_version"] = "invalid-publication-result"
            return result

        with mock.patch.object(self.interface, "_publish_scientific_context", side_effect=corrupt_result):
            response = self._execute(RECORD_CONTEXT, request, executive_epoch_id=self.epoch)
        self.assertEqual(response["status"], "unavailable", response)
        self.assertEqual(response["error"]["code"], "mission_operation_unavailable")
        self.assertEqual(response["error"]["correction"], "repair_owner_interface")
        retained = self.store.read_scientific_publication_result(
            mission_id="mission.1", command_id=published["command_id"],
        )
        self.assertEqual(retained["schema_version"], "mathematical_research.scientific_publication_result.v1")
        self.assertEqual(retained["context_references"], tuple(published["context_references"]))
        metadata = deep_thaw(self.store.read_metadata())
        invalid = self._execute(RECORD_CONTEXT, {**request, "not_in_contract": True}, executive_epoch_id=self.epoch)
        self.assertEqual(invalid["status"], "rejected", invalid)
        self.assertEqual(invalid["error"]["code"], "mission_operation_request_invalid")
        self.assertEqual(invalid["error"]["correction"], "revise_request")
        self.assertEqual(deep_thaw(self.store.read_metadata()), metadata)

    def test_all_member_noop_journals_exact_acknowledgement_without_owner_revision(self):
        self._open()
        before = self._reference("science")
        cut = self.store.read_metadata()["current_project_commit"]
        request = {"mode": "scientific", "updates": [self._patch("science")], "exposure_required": []}
        response = self._execute(RECORD_CONTEXT, request, executive_epoch_id=self.epoch)
        self.assertEqual(response["status"], "completed", response)
        result = response["result"]
        self.assertTrue(result["semantic_noop"])
        self.assertEqual(result["project_commit"], cut + 1)
        self.assertEqual(self._reference("science"), before)
        self._context([self._patch("science", insert={"b": self._treatment("An unrelated question.")})])
        replay = self._execute(RECORD_CONTEXT, request, executive_epoch_id=self.epoch)["result"]
        self.assertEqual(replay["project_commit"], result["project_commit"])
        self.assertEqual(replay["context_references"], [before])
        self.assertTrue(replay["replayed"])

    def test_paired_deeper_question_survives_unrelated_patch_and_exact_replay(self):
        self._open(bound=True)
        request = self._interpretation(continuation=self._continuation(deeper=True))
        response = self._execute(INTERPRET_MATERIAL, request, executive_epoch_id=self.epoch)
        self.assertEqual(response["status"], "completed", response)
        publication = response["result"]["publication"]
        source = read_context_revision(self.store, context_id="consumer").record.document["treatments"]["a"]["sources"][0]
        self.assertEqual(deep_thaw(source["reference"]), publication["evidence_reference"])
        self._context([self._patch("consumer", insert={"b": self._treatment("Independent B remains open.")})])
        current = read_context_revision(self.store, context_id="consumer")
        self.assertEqual(set(current.record.document["treatments"]), {"a", "b"})
        replay = self._execute(INTERPRET_MATERIAL, request, executive_epoch_id=self.epoch)
        self.assertEqual(replay["status"], "completed", replay)
        self.assertEqual(replay["result"]["publication"]["context_references"], publication["context_references"])
        self.assertEqual(replay["result"]["publication"]["project_commit"], publication["project_commit"])
        self.assertEqual(self._reference("consumer")["revision"], current.revision)
        result_read = self._execute(RETRIEVE, {"mode": "publication_result", "command_id": publication["command_id"]},
                                    executive_epoch_id=self.epoch)
        self.assertEqual(result_read["status"], "completed", result_read)
        self.assertEqual(result_read["result"]["context_references"], publication["context_references"])
        checkpoint = self._execute(CHECKPOINT, {}, executive_epoch_id=self.epoch)
        self.assertEqual(checkpoint["status"], "completed", checkpoint)
        self.goal_thread_id = "goal-thread:scientific-publication-after-b"
        self.goal_workspace = self.goal_workspace.parent / "scientific-after-b"
        self.goal_workspace.mkdir()
        self.epoch = self._authorize()
        self._bind(self.epoch)
        science = self.interface.executive_orientation()["scientific_context"]
        exposed = {(item["context_reference"]["identity"], item["treatment_id"]): item for item in science["treatments"]}
        self.assertIn(("consumer", "a"), exposed)
        self.assertNotIn(("consumer", "b"), exposed)
        self.assertIn("Lower gaps", exposed[("consumer", "a")]["content"]["qualifications"][0])
        successor_read = self._execute(RETRIEVE, {"mode": "publication_result", "command_id": publication["command_id"]},
                                       executive_epoch_id=self.epoch)
        self.assertEqual(successor_read["status"], "completed", successor_read)
        self.assertEqual(successor_read["result"]["project_commit"], publication["project_commit"])

    def test_paired_publication_rejects_real_metadata_and_unrelated_treatment_changes(self):
        self._open(bound=True)
        self._context([self._patch("science", insert={"unrelated": self._treatment()})])
        before = self._reference("science")
        for change in ("metadata", "unrelated_treatment"):
            with self.subTest(change=change):
                continuation = self._continuation()
                patch = continuation["updates"][0]["patch"]
                if change == "metadata":
                    patch["metadata_replace"] = {"restrictions": ["A material but unrelated restriction."]}
                    message = "unrelated Context metadata"
                else:
                    patch["replace"] = {"unrelated": self._treatment("A genuinely changed independent account.")}
                    message = "unrelated treatment change"
                response = self._execute(
                    INTERPRET_MATERIAL, self._interpretation(change, continuation=continuation),
                    executive_epoch_id=self.epoch,
                )
                self.assertEqual(response["status"], "rejected", response)
                self.assertIn(message, response["error"]["message"])
                self.assertEqual(self._reference("science"), before)
                with self.assertRaises(StaleCommandError):
                    read_evidence_meaning(self.store, evidence_id=change)

    def test_missing_exposure_is_atomic_and_custody_survives_rejection(self):
        self._open(bound=True)
        continuation = self._continuation(deeper=True)
        continuation["updates"] = continuation["updates"][:1]
        response = self._execute(INTERPRET_MATERIAL, self._interpretation(continuation=continuation), executive_epoch_id=self.epoch)
        self.assertEqual(response["status"], "rejected", response)
        with self.assertRaises(StaleCommandError):
            read_evidence_meaning(self.store, evidence_id="ingredient")
        with self.assertRaises(StaleCommandError):
            read_context_revision(self.store, context_id="consumer")
        self.assertGreater(len(self.store.list_mission_raw_captures("mission.1")), 0)

    def test_unchanged_member_does_not_suppress_changed_sibling(self):
        self._open()
        before = self._reference("science")
        result = self._context([self._patch("science"), self._create("other", self._document("other", {"a": self._treatment()}))])
        self.assertFalse(result["semantic_noop"])
        self.assertEqual(self._reference("science"), before)
        self.assertEqual(self._reference("other")["revision"], 1)

    def _superseded_multi_context_publication(self):
        self._open()
        original = self._context([
            self._patch("science", insert={"a": self._treatment()}),
            self._create("other", self._document("other", {"a": self._treatment()})),
        ])
        self.assertEqual(len(original["context_references"]), 2)
        successor = self._context([
            self._patch("science", insert={"b": self._treatment("A separate successor question.")}),
            self._patch("other", insert={"b": self._treatment("Another successor question.")}),
        ])
        with self.store._connection(read_only=True) as connection:
            for publication in (original, successor):
                row = connection.execute(
                    "SELECT changed_heads_json FROM transition_journal WHERE project_commit_no = ?",
                    (publication["project_commit"],),
                ).fetchone()
                self.assertEqual(len(json.loads(str(row["changed_heads_json"]))), 2)
        return original, successor

    @staticmethod
    def _exact_context_reference(reference):
        return RevisionRef(
            TypedWorkspaceId(IdentityKind.CONTEXT, reference["identity"]),
            reference["revision"],
        )

    def test_multi_context_historical_origin_uses_sealed_index_without_journal_scan(self):
        original, _ = self._superseded_multi_context_publication()
        with mock.patch.object(
            workspace_store, "_validated_journal_index",
            side_effect=AssertionError("An exact historical read must not rebuild the journal index."),
        ), mock.patch.object(
            workspace_store, "_validated_transition_journal_chain",
            side_effect=AssertionError("An exact historical read must not scan the journal chain."),
        ), mock.patch.object(
            workspace_store, "_schema12_indexed_typed_origin_journal",
            wraps=workspace_store._schema12_indexed_typed_origin_journal,
        ) as indexed_origin:
            for reference in original["context_references"]:
                with self.subTest(context=reference["identity"]):
                    stored = self.store.get_revision(self._exact_context_reference(reference))
                    self.assertIsNotNone(stored)
                    self.assertEqual(stored.payload_digest, reference["payload_sha256"])
                    self.assertEqual(stored.reference.revision, reference["revision"])
                    self.assertEqual(set(stored.payload["treatments"]), {"a"})
            self.assertGreaterEqual(indexed_origin.call_count, 2)

    def test_multi_context_historical_origin_rejects_missing_index_path_without_fallback(self):
        original, _ = self._superseded_multi_context_publication()
        # Remove only the current sealed index's manifest root in this test's
        # disposable Store. Exact owner rows, commands and journals remain.
        with closing(sqlite3.connect(self.store.paths.database)) as connection:
            row = connection.execute(
                "SELECT owner_content_index_json FROM transition_journal WHERE project_commit_no = "
                "(SELECT current_project_commit FROM workspace_metadata WHERE singleton = 1)",
            ).fetchone()
            descriptor = json.loads(row[0])
            removed = connection.execute(
                "DELETE FROM owner_content_index_node WHERE node_digest = ?",
                (descriptor["root"]["digest"],),
            )
            self.assertEqual(removed.rowcount, 1)
            connection.commit()
        with mock.patch.object(
            workspace_store, "_validated_journal_index",
            side_effect=AssertionError("Corruption must not invoke a whole-history fallback."),
        ), mock.patch.object(
            workspace_store, "_validated_transition_journal_chain",
            side_effect=AssertionError("Corruption must not invoke a journal scan."),
        ), self.assertRaisesRegex(WorkspaceIntegrityError, "referenced object is absent"):
            self.store.get_revision(self._exact_context_reference(original["context_references"][0]))

    def test_multi_context_historical_origin_rejects_swapped_posting_origin(self):
        original, successor = self._superseded_multi_context_publication()
        reference = next(item for item in original["context_references"] if item["identity"] == "science")
        exact_key = ("context", reference["identity"], reference["revision"])
        original_posting = owner_content_index.OwnerContentIndex.posting
        substituted = []

        def swapped_origin(index, directory_key, key):
            posting = original_posting(index, directory_key, key)
            if directory_key[:4] == ("owner_inventory", "project.rh", "mission.1", "retained_history") and key == exact_key:
                self.assertIsNotNone(posting)
                self.assertEqual(posting.origin_commit, original["project_commit"])
                substituted.append(key)
                # Preserve internally coherent source/selection metadata so the
                # real Store must reject against the other commit's exact row
                # binding, not merely inconsistent fields in this test double.
                return replace(
                    posting, origin_commit=successor["project_commit"],
                    source_origin_commit=successor["project_commit"],
                    metadata={**dict(posting.metadata), "selected_at_commit": successor["project_commit"]},
                )
            return posting

        with mock.patch.object(
            owner_content_index.OwnerContentIndex, "posting", autospec=True,
            side_effect=swapped_origin,
        ), mock.patch.object(
            workspace_store, "_validated_journal_index",
            side_effect=AssertionError("A forged origin must not trigger history reconstruction."),
        ), mock.patch.object(
            workspace_store, "_validated_transition_journal_chain",
            side_effect=AssertionError("A forged origin must not trigger a journal scan."),
        ), self.assertRaisesRegex(
            WorkspaceIntegrityError,
            "indexed historical typed origin differs from its exact journal",
        ):
            self.store.get_revision(self._exact_context_reference(reference))
        self.assertTrue(substituted)

    def test_multi_context_full_audit_derives_origins_without_index_under_audit(self):
        self._superseded_multi_context_publication()
        with mock.patch.object(
            workspace_store, "_schema12_indexed_typed_origin_journal",
            side_effect=AssertionError("The full audit must derive typed origins from its authenticated journal."),
        ) as indexed_origin:
            report = self.store.verify_integrity()
        self.assertIsNotNone(report)
        indexed_origin.assert_not_called()

    def test_valid_older_reference_cannot_be_substituted_before_command_issuance(self):
        self._open()
        old_reference = self._reference("science")
        before = deep_thaw(self.store.read_metadata())
        original = self.store._apply_successor_storage_command
        def substitute_receipt(**kwargs):
            receipt = deep_thaw(kwargs["scientific_publication_receipt"])
            receipt["context_references"][0] = old_reference
            receipt["context_members"][0]["reference"] = old_reference
            kwargs["scientific_publication_receipt"] = receipt
            return original(**kwargs)
        with mock.patch.object(self.store, "_apply_successor_storage_command", side_effect=substitute_receipt):
            response = self._execute(RECORD_CONTEXT, {"mode": "scientific",
                "updates": [self._patch("science", insert={"a": self._treatment()})], "exposure_required": []},
                executive_epoch_id=self.epoch)
        self.assertEqual(response["status"], "unavailable", response)
        self.assertEqual(response["error"]["code"], "mission_operation_unavailable")
        self.assertEqual(response["error"]["correction"], "repair_owner_interface")
        self.assertEqual(self._reference("science"), old_reference)
        self.assertEqual(deep_thaw(self.store.read_metadata()), before)

    def test_changed_request_digest_conflicts_on_committed_publication_read_and_replay_without_writes(self):
        self._open()
        previous = read_context_revision(self.store, context_id="science")
        update = self._patch("science", insert={"a": self._treatment()})
        authority = self.interface._direct_epoch_authority(self.epoch)
        self.assertIsNotNone(authority)
        prepared = prepare_scientific_context_update(
            authority=authority, update=update, previous=previous,
        )
        result = self._context([update])
        reference = self._reference("science")
        metadata = deep_thaw(self.store.read_metadata())
        with self.store._connection(read_only=True) as connection:
            row = connection.execute(
                "SELECT actor, result_json FROM command_result WHERE command_id = ?",
                (result["command_id"],),
            ).fetchone()
            actor = str(row["actor"])
            original_digest = json.loads(str(row["result_json"]))["result"]["scientific_publication"]["request_sha256"]
            counts_before = tuple(connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
                                  for table in ("project_commit", "transition_journal", "command_result",
                                                "context_revision", "evidence_item_revision"))
        changed_digest = ("0" if original_digest[0] != "0" else "1") + original_digest[1:]
        with self.assertRaisesRegex(CommandConflictError, "request differs"):
            self.store.read_scientific_publication_result(
                mission_id="mission.1", command_id=result["command_id"], request_sha256=changed_digest,
            )
        # Reconstruct from the original immutable preimage, not today's head,
        # so the altered envelope must conflict before a stale-target check.
        with self.assertRaisesRegex(CommandConflictError, "different complete command envelope"):
            self.store.commit_scientific_context_publication(
                executive_epoch_id=self.epoch, mission_id="mission.1", updates=(prepared,),
                exposure_required=(), request_sha256=changed_digest,
                lease=self.interface._writer_lease(), command_id=result["command_id"], actor=actor,
                expected_canonical_authority_digest=authority.canonical_authority_digest,
            )
        self.assertEqual(deep_thaw(self.store.read_metadata()), metadata)
        self.assertEqual(self._reference("science"), reference)
        with self.store._connection(read_only=True) as connection:
            counts_after = tuple(connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
                                 for table in ("project_commit", "transition_journal", "command_result",
                                               "context_revision", "evidence_item_revision"))
        self.assertEqual(counts_after, counts_before)
        exact = self.store.read_scientific_publication_result(
            mission_id="mission.1", command_id=result["command_id"], request_sha256=original_digest,
        )
        self.assertEqual(exact["project_commit"], result["project_commit"])
        self.assertEqual(deep_thaw(exact["context_references"]), result["context_references"])

    def test_publication_result_fails_closed_when_original_historical_context_is_corrupt(self):
        self._open()
        result = self._context([self._patch("science", insert={"a": self._treatment()})])
        original = result["context_references"][0]
        self._context([self._patch("science", insert={"b": self._treatment("Independent B remains open.")})])
        self.assertGreater(self._reference("science")["revision"], original["revision"])
        with closing(sqlite3.connect(self.store.paths.database)) as connection:
            result_before = connection.execute(
                "SELECT result_digest, result_json, project_commit_no FROM command_result WHERE command_id = ?",
                (result["command_id"],),
            ).fetchone()
            self.assertIsNotNone(result_before)
            # Deliberate corruption of this test's disposable historical row.
            # Keep the current head and the existing result/journal untouched.
            changed = connection.execute(
                "UPDATE context_revision SET payload_json = ? WHERE object_id = ? AND revision = ?",
                ("{}", original["identity"], original["revision"]),
            )
            self.assertEqual(changed.rowcount, 1)
            connection.commit()
        with self.assertRaises(WorkspaceIntegrityError):
            self.store.read_scientific_publication_result(
                mission_id="mission.1", command_id=result["command_id"],
            )
        with closing(sqlite3.connect(self.store.paths.database)) as connection:
            result_after = connection.execute(
                "SELECT result_digest, result_json, project_commit_no FROM command_result WHERE command_id = ?",
                (result["command_id"],),
            ).fetchone()
            corrupted = connection.execute(
                "SELECT payload_json FROM context_revision WHERE object_id = ? AND revision = ?",
                (original["identity"], original["revision"]),
            ).fetchone()
        self.assertEqual(result_after, result_before)
        self.assertEqual(corrupted, ("{}",))

    def test_receipt_cannot_omit_membership_only_journal_effect(self):
        self._open(bound=True)
        pair = {"context_id": "other", "treatment_id": "a"}
        self._context([self._create("other", self._document("other", {"a": self._treatment()}))])
        result = self._context([self._patch("science", add=[pair])], required=[pair])
        self.assertEqual(result["changed_treatments"], [])
        with self.store._connection(read_only=True) as connection:
            row = connection.execute("SELECT result_json FROM command_result WHERE command_id = ?",
                                     (result["command_id"],)).fetchone()
            receipt = json.loads(str(row["result_json"]))["result"]["scientific_publication"]
            receipt["context_references"] = []
            receipt["context_members"] = []
            with self.assertRaisesRegex(WorkspaceIntegrityError, "effect set"):
                _scientific_publication_result_from_receipt(
                    self.store, connection, receipt=receipt, command_id=result["command_id"],
                    project_commit=result["project_commit"], replayed=True,
                )

    def test_authored_selected_treatment_removal_requires_root_membership_adjustment(self):
        self._open(bound=True)
        pair = {"context_id": "other", "treatment_id": "a"}
        self._context([self._create("other", self._document("other", {"a": self._treatment()})),
                       self._patch("science", add=[pair])], required=[pair])
        removal = self._patch("other", remove=[{"treatment_id": "a", "reason": "Move to a qualified replacement."}])
        response = self._execute(RECORD_CONTEXT, {"mode": "scientific", "updates": [removal], "exposure_required": []},
                                 executive_epoch_id=self.epoch)
        self.assertEqual(response["status"], "rejected", response)
        self.assertIn("a", read_context_revision(self.store, context_id="other").record.document["treatments"])
        self._context([removal, self._patch("science", drop=[{**pair, "reason": "Explicitly remove the moved membership."}])])

    def test_after_commit_acknowledgement_failure_replays_without_new_science(self):
        self._open(bound=True)
        request = self._interpretation(continuation=self._continuation())
        def fail_publication_ack(command_id):
            if command_id.startswith("scientific-publication:"):
                raise ValueError("Injected loss of publication acknowledgement.")
        with mock.patch.object(self.store, "_after_commit", side_effect=fail_publication_ack):
            failed = self._execute(INTERPRET_MATERIAL, request, executive_epoch_id=self.epoch)
        self.assertEqual(failed["status"], "unavailable", failed)
        self.assertEqual(failed["error"]["code"], "mission_operation_unavailable")
        self.assertEqual(failed["error"]["correction"], "repair_owner_interface")
        self.assertIn("committed, but its post-COMMIT acknowledgement failed", failed["error"]["message"])
        exact = self._reference("science")
        evidence = read_evidence_meaning(self.store, evidence_id="ingredient")
        replay = self._execute(INTERPRET_MATERIAL, request, executive_epoch_id=self.epoch)
        self.assertEqual(replay["status"], "completed", replay)
        self.assertTrue(replay["result"]["publication"]["replayed"])
        self.assertEqual(self._reference("science"), exact)
        self.assertEqual(read_evidence_meaning(self.store, evidence_id="ingredient").revision, evidence.revision)

    def test_transaction_failure_publishes_neither_member_and_keeps_custody(self):
        self._open(bound=True)
        request = self._interpretation(continuation=self._continuation())
        before = self._reference("science")
        validate = workspace_store._validate_scientific_publication_transaction

        def fail_publication_only(store, connection, source):
            if source.command_kind == "commit_scientific_context_publication":
                raise ValueError("Injected transaction validation failure.")
            return validate(store, connection, source)

        with mock.patch("research_core.workspace_store._validate_scientific_publication_transaction",
                        side_effect=fail_publication_only):
            failed = self._execute(INTERPRET_MATERIAL, request, executive_epoch_id=self.epoch)
        self.assertEqual(failed["status"], "rejected", failed)
        self.assertEqual(self._reference("science"), before)
        with self.assertRaises(StaleCommandError):
            read_evidence_meaning(self.store, evidence_id="ingredient")
        captures = tuple(self.store.list_mission_raw_captures("mission.1"))
        self.assertTrue(captures)
        retried = self._execute(INTERPRET_MATERIAL, request, executive_epoch_id=self.epoch)
        self.assertEqual(retried["status"], "completed", retried)
        self.assertEqual(len(self.store.list_mission_raw_captures("mission.1")), len(captures))

    def test_synthesis_replay_precedes_resolution_of_advanced_inputs_and_target(self):
        self._open(bound=True)
        for identity in ("input-left", "input-right"):
            response = self._execute(INTERPRET_MATERIAL, self._interpretation(identity), executive_epoch_id=self.epoch)
            self.assertEqual(response["status"], "completed", response)
        consequence = {"kind": "evidence", "evidence_id": "evidence:combined", "expected_head": None,
                       "statement": "Composition remains an unfinished scientific question.",
                       "scope": "Two exact finite ingredients.", "strength": "heuristic",
                       "semantic_role": "non_exclusion", "non_inferences": ["No composed bound is proved."],
                       "scientific_continuation": self._continuation()}
        request = {"inputs": [{"id": "evidence:input-left", "role": "first ingredient"},
                              {"id": "evidence:input-right", "role": "second ingredient"}],
                   "relation_question": "Can the two finite ingredients compose?",
                   "compatibility_analysis": "The consumer normalization still requires proof.",
                   "derivation_or_incompatibility": "The local lower gaps do not prove an upper mismatch estimate.",
                   "scope": "Finite ingredients only.", "strength": "heuristic",
                   "edge_survival": "The original local ingredients survive.", "consequences": [consequence]}
        response = self._execute(SYNTHESIZE, request, executive_epoch_id=self.epoch)
        self.assertEqual(response["status"], "completed", response)
        self.assertEqual(response["result"]["rejections"], [])
        original = response["result"]["records"][0]["publication"]
        for identity in ("input-left", "input-right", "combined"):
            advanced = self._interpretation(identity)
            advanced["interpretation"] = "A later separately scoped correction preserves only the finite diagnostic."
            changed = self._execute(INTERPRET_MATERIAL, advanced, executive_epoch_id=self.epoch)
            self.assertEqual(changed["status"], "completed", changed)
        with mock.patch.object(self.interface, "_prepare_semantic_synthesis_inputs",
                               side_effect=AssertionError("Replay must not resolve current synthesis inputs.")):
            replay = self._execute(SYNTHESIZE, request, executive_epoch_id=self.epoch)
        self.assertEqual(replay["status"], "completed", replay)
        self.assertEqual(replay["result"]["rejections"], [])
        repeated = replay["result"]["records"][0]["publication"]
        self.assertEqual(repeated["project_commit"], original["project_commit"])
        self.assertEqual(repeated["evidence_reference"], original["evidence_reference"])
        self.assertEqual(read_evidence_meaning(self.store, evidence_id="combined").revision, 2)

    def test_unchanged_evidence_does_not_suppress_new_context_treatment(self):
        self._open(bound=True)
        ordinary = self._interpretation()
        response = self._execute(INTERPRET_MATERIAL, ordinary, executive_epoch_id=self.epoch)
        self.assertEqual(response["status"], "completed", response)
        prior = read_evidence_meaning(self.store, evidence_id="ingredient")
        paired = {**ordinary, "scientific_continuation": self._continuation(), "expected_head": {
            "kind": "evidence", "identity": "ingredient", "revision": prior.revision, "payload_sha256": prior.payload_digest}}
        response = self._execute(INTERPRET_MATERIAL, paired, executive_epoch_id=self.epoch)
        self.assertEqual(response["status"], "completed", response)
        result = response["result"]["publication"]
        self.assertFalse(result["semantic_noop"])
        self.assertEqual(result["evidence_reference"], paired["expected_head"])
        self.assertEqual(read_evidence_meaning(self.store, evidence_id="ingredient").revision, prior.revision)
        treatment = read_context_revision(self.store, context_id="science").record.document["treatments"]["a"]
        self.assertEqual(deep_thaw(treatment["sources"][0]["reference"]), paired["expected_head"])

    def _synthesis_sibling_request(self, continuation):
        inputs = []
        for identity in ("left", "right"):
            response = self._execute(RECORD_CANDIDATE, {
                "candidate_id": f"candidate:{identity}", "proposal_kind": "lemma",
                "exact_statement": f"The {identity} fixture remains an open local proposal.",
                "standing": {"status": "open", "basis": "Independent checking remains."},
                "non_inferences": ["No RH conclusion."],
            }, executive_epoch_id=self.epoch)
            self.assertEqual(response["status"], "completed", response)
            inputs.append({"id": response["result"]["record_id"], "role": identity})
        independent = {"kind": "evidence", "evidence_id": "evidence:independent-before",
                       "statement": "The two proposals do not establish their composition.",
                       "scope": "The exact two fixture Candidates.", "strength": "heuristic",
                       "semantic_role": "non_exclusion", "non_inferences": ["No complete proof."]}
        paired = {**independent, "evidence_id": "evidence:paired-sibling", "expected_head": None,
                  "scientific_continuation": continuation}
        complete = {"kind": "candidate", "candidate": {
            "candidate_id": "candidate:purported-complete", "proposal_kind": "proof_architecture",
            "exact_statement": "Every nontrivial zero of the Riemann zeta function has real part one half.",
            "mechanism": "A purported unconditional proof supplied for adversarial reconstruction.",
            "scope_and_reach": "A purported complete proof of the Riemann Hypothesis.",
            "complete_target_claim": {"target": "riemann_hypothesis", "disposition": "proof"},
            "standing": {"status": "open", "basis": "Not independently reviewed."},
            "non_inferences": ["Preservation does not establish validity."],
        }}
        return {"inputs": inputs, "relation_question": "What remains to establish composition?",
                "compatibility_analysis": "Neither local proposal supplies the missing bridge.",
                "derivation_or_incompatibility": "The combined claim remains unproved.",
                "scope": "These two local Candidates.", "strength": "heuristic",
                "edge_survival": "The original local questions survive.",
                "consequences": [independent, paired, complete,
                                 {**independent, "evidence_id": "evidence:independent-after"}]}

    def test_malformed_paired_sibling_does_not_block_candidate_a1_or_later_evidence(self):
        self._open(bound=True)
        request = self._synthesis_sibling_request({"updates": "not-a-valid-list", "exposure_required": []})
        response = self._execute(SYNTHESIZE, request, executive_epoch_id=self.epoch)
        self.assertEqual(response["status"], "completed", response)
        self.assertEqual([item["consequence_index"] for item in response["result"]["rejections"]], [1])
        self.assertEqual(len(response["result"]["records"]), 3)
        self.assertTrue(any(item["candidate_ref"]["identity"] == "purported-complete"
                            for item in self.interface.host_snapshot()["current_state"]["open_candidate_a1"]))
        self.assertEqual(read_evidence_meaning(self.store, evidence_id="independent-after").revision, 1)

    def test_committed_paired_acknowledgement_loss_preserves_siblings_and_exact_replay(self):
        self._open(bound=True)
        request = self._synthesis_sibling_request(self._continuation())

        def fail_publication_ack(command_id):
            if command_id.startswith("scientific-publication:"):
                raise ValueError("Injected loss of publication acknowledgement.")

        with mock.patch.object(self.store, "_after_commit", side_effect=fail_publication_ack):
            response = self._execute(SYNTHESIZE, request, executive_epoch_id=self.epoch)
        self.assertEqual(response["status"], "completed", response)
        self.assertEqual([item["consequence_index"] for item in response["result"]["rejections"]], [1])
        self.assertIn("committed, but its post-COMMIT acknowledgement failed",
                      response["result"]["rejections"][0]["message"])
        self.assertEqual(len(response["result"]["records"]), 3)
        self.assertTrue(any(item["candidate_ref"]["identity"] == "purported-complete"
                            for item in self.interface.host_snapshot()["current_state"]["open_candidate_a1"]))
        evidence_ids = ("independent-before", "paired-sibling", "independent-after")
        retained = {identity: read_evidence_meaning(self.store, evidence_id=identity)
                    for identity in evidence_ids}
        self.assertEqual([item.revision for item in retained.values()], [1, 1, 1])
        exact = self._reference("science")
        replay = self._execute(SYNTHESIZE, request, executive_epoch_id=self.epoch)
        self.assertEqual(replay["status"], "completed", replay)
        self.assertEqual(replay["result"]["rejections"], [])
        self.assertEqual(len(replay["result"]["records"]), 4)
        publication = replay["result"]["records"][1]["publication"]
        self.assertTrue(publication["replayed"])
        self.assertEqual(publication["context_references"], [exact])
        self.assertEqual(publication["evidence_reference"], {
            "kind": "evidence", "identity": "paired-sibling",
            "revision": retained["paired-sibling"].revision,
            "payload_sha256": retained["paired-sibling"].payload_digest,
        })
        self.assertEqual(self._reference("science"), exact)
        for identity, original in retained.items():
            current = read_evidence_meaning(self.store, evidence_id=identity)
            self.assertEqual((current.revision, current.payload_digest),
                             (original.revision, original.payload_digest))


if __name__ == "__main__":
    unittest.main()
