from __future__ import annotations

import base64
import contextlib
import functools
import hashlib
import inspect
import json
import os
import re
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

PACKAGE_ROOT = Path(__file__).resolve().parents[1]
TEST_ROOT = Path(__file__).resolve().parent
for path in (PACKAGE_ROOT, TEST_ROOT):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

from jsonschema import Draft7Validator
import test_mission_interface_direct as fixtures
import test_mission_retrieval as retrieval_fixtures
from research_core import mission_observation, workspace_store
from research_core.evidence_store import DeletionDirective
from research_core.mission_evidence import CaptureScope
from research_core.mission_operation_contract import CHECKPOINT, RECORD_STRATEGY
from research_core.research_model import deep_thaw
from research_core.workspace_store import RawCaptureArtifactUnavailableError, StaleCommandError, WorkspaceIntegrityError

REPO_ROOT = PACKAGE_ROOT.parents[1]
SCHEMA_PATH = REPO_ROOT / "contracts/schemas/mission_observation.v1.schema.json"


class MissionObservationTests(unittest.TestCase):
    setUpClass = classmethod(fixtures.DirectMissionInterfaceTests.setUpClass.__func__)
    setUp = fixtures.DirectMissionInterfaceTests.setUp
    _request = staticmethod(fixtures.DirectMissionInterfaceTests._request)
    _authorize = fixtures.DirectMissionInterfaceTests._authorize
    _bind = fixtures.DirectMissionInterfaceTests._bind
    _execute = fixtures.DirectMissionInterfaceTests._execute
    _prepare_exact_retrieval_fixture = fixtures.DirectMissionInterfaceTests._prepare_exact_retrieval_fixture
    _source_evidence = retrieval_fixtures.RootRetrievalTests._source_evidence
    key = "ad" * 32

    def _observe(self, operation, **selection):
        if operation == "exact_record" and "source_binding" not in selection:
            selection["source_binding"] = self._observe("current_decision")["source_binding"]
        value = deep_thaw(self.interface.observe_mission({"operation": operation, **selection}, cursor_mac_key=self.key))
        Draft7Validator(json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))).validate(value)
        self.assertNotIn(self.key, json.dumps(value))
        return value

    def _strategy(self, epoch, name):
        result = self._execute(RECORD_STRATEGY, {
            "mission_continuation": "continue", "integrated_comparison": name,
            "reconsideration_conditions": [{"condition": "Only the exact new finding changes this decision."}],
        }, executive_epoch_id=epoch)
        self.assertEqual(result["status"], "completed", result)

    def _bind_independent_scientific_context(self, epoch):
        from test_executive_orientation import ExecutiveOrientationTests

        context = ExecutiveOrientationTests._scientific_context(self, epoch, "context.observer.science", {
            "parent": {"question": "Which qualified result survives a scoped correction?",
                       "account": "The parent question remains open; the finite result survives.",
                       "qualifications": ["Finite support does not establish a uniform bound."], "sources": []},
        })  # Root-local treatments are exposed without cross-Context selectors.
        checkpoint = self._execute(CHECKPOINT, {}, executive_epoch_id=epoch)
        self.assertEqual(checkpoint["status"], "completed", checkpoint)
        mission, _ = self.interface._resolve_owner_selector({"id": f"mission:{self.interface._mission_id}"})
        self.interface.bind_scientific_context_from_owner(
            expected_mission_revision=mission["revision"], expected_mission_payload_sha256=mission["payload_sha256"],
            expected_canonical_authority_digest=self.store.read_metadata()["canonical_authority_digest"],
            context_id="context.observer.science", expected_context_revision=context.revision,
            expected_context_payload_sha256=context.payload_digest,
        )
        return context

    def test_current_catalog_and_exported_contract_are_owner_derived_without_writes(self):
        for request in ({"operation": []}, {"operation": {}}, {"operation": "catalog", "collection": []},
                        {"operation": "current_decision", "source_binding": None},
                        {"operation": "exact_record", "handle": "mission:mission.1@1"}):
            with self.assertRaises(ValueError):
                mission_observation.validate_observation_request(request)
        epoch = self._authorize()
        self._bind(epoch)
        context = self._bind_independent_scientific_context(epoch)
        before = deep_thaw(self.store.read_metadata())
        with mock.patch.object(self.store, "claim_writer", side_effect=AssertionError("observer acquired writer")), \
             mock.patch.object(self.interface, "execute_semantic_operation", side_effect=AssertionError("observer borrowed executive")), \
             mock.patch.object(self.interface, "reconstruct", side_effect=AssertionError("observer built panorama")), \
             mock.patch.object(self.store, "read_raw_capture_artifact_bytes", side_effect=AssertionError("catalog read raw body")):
            current = self._observe("current_decision")
            self.assertEqual(current["state"], "present")
            decision = current["items"][0]
            self.assertEqual(decision["mission"]["handle"], "mission:mission.1@2")
            self.assertEqual(decision["strategy"]["handle"], "strategy:strategy.theta.1@1")
            self.assertNotIn("execution_policy", decision["mission"]["semantic"])
            self.assertEqual(decision["strategy"]["semantic"]["integrated_comparison"], self.genesis_seed["strategy"]["integrated_comparison"])
            self.assertNotIn("context.observer.science", json.dumps(decision["strategy"]["semantic"]))
            owners = self._observe("catalog", collection="owner_records")["items"]
            scientific = [row for row in owners if row["source_field"] == "scientific_context"]
            self.assertEqual(len(scientific), 1)
            self.assertEqual(scientific[0]["value"]["reference"]["payload_sha256"], context.payload_digest)
            exact_context = self._observe("exact_record", handle=scientific[0]["value"]["handle"], source_binding=current["source_binding"])
            self.assertIn("Finite support does not establish a uniform bound.",
                base64.b64decode(exact_context["items"][0]["content_base64"]).decode("utf-8"))
            for collection in sorted(mission_observation.COLLECTIONS):
                result = self._observe("catalog", collection=collection, page_size=1)
                self.assertEqual(result["source_binding"], current["source_binding"])
                self.assertEqual(result["coverage"]["collection"], collection)
        self.assertEqual(deep_thaw(self.store.read_metadata()), before)

    def test_fixed_cut_strategy_pages_and_exact_reads_survive_unrelated_writes(self):
        epoch = self._authorize()
        self._bind(epoch)
        self._strategy(epoch, "Second authored decision.")
        first = self._observe("decision_history", collection="strategies", page_size=1)
        self.assertIsNotNone(first["next_cursor"])
        self._strategy(epoch, "Third authored decision must not splice into the fixed cut.")
        before = deep_thaw(self.store.read_metadata())
        second = self._observe("decision_history", collection="strategies", page_size=1,
                               source_binding=first["source_binding"], cursor=first["next_cursor"])
        self.assertIsNone(second["next_cursor"])
        self.assertEqual([first["items"][0]["after"]["revision"], second["items"][0]["after"]["revision"]], [1, 2])
        self.assertEqual(second["items"][0]["before"], first["items"][0]["after"])
        self.assertEqual(second["items"][0]["temporal_status"], "current_at_cut")
        exact = self._observe("exact_record", handle="strategy:strategy.theta.1@2", source_binding=first["source_binding"])
        self.assertEqual(json.loads(base64.b64decode(exact["items"][0]["content_base64"]))["integrated_comparison"], "Second authored decision.")
        later = self._observe("exact_record", handle="strategy:strategy.theta.1@3", source_binding=first["source_binding"])
        self.assertEqual(later["state"], "unavailable")
        for binding in ({**first["source_binding"], "root_identity": "another-incarnation"},
                        {**first["source_binding"], "mission_id": "another-mission"}):
            self.assertEqual(self._observe("exact_record", handle="strategy:strategy.theta.1@2", source_binding=binding)["state"], "expired")
        changed = self._observe("decision_history", collection="strategies", page_size=2,
                                source_binding=first["source_binding"], cursor=first["next_cursor"])
        self.assertEqual(changed["state"], "expired")
        corrupted = self._observe("decision_history", collection="strategies", page_size=1,
                                  source_binding=first["source_binding"], cursor="broken")
        self.assertEqual(corrupted["state"], "expired")
        self.assertEqual(deep_thaw(self.store.read_metadata()), before)

    def test_checkpoint_history_uses_exact_predecessors_and_bounded_scan_pages(self):
        first_epoch = self._authorize()
        self._bind(first_epoch)
        self._strategy(first_epoch, "First checkpoint ground.")
        first = self._execute(CHECKPOINT, {}, executive_epoch_id=first_epoch)
        self.assertEqual(first["status"], "completed", first)
        fixed = self._observe("current_decision")["source_binding"]
        self.goal_thread_id = "goal-thread:observation-successor"
        second_epoch = self._authorize()
        self._bind(second_epoch)
        self._strategy(second_epoch, "Second checkpoint ground.")
        second = self._execute(CHECKPOINT, {}, executive_epoch_id=second_epoch)
        self.assertEqual(second["status"], "completed", second)
        history = self._observe("decision_history", collection="checkpoints", page_size=1)
        self.assertEqual(history["items"][0]["after"]["checkpoint_id"], second["result"]["checkpoint_id"])
        self.assertEqual(history["items"][0]["before"]["checkpoint_id"], first["result"]["checkpoint_id"])
        older = self._observe("decision_history", collection="checkpoints", page_size=1,
                              source_binding=history["source_binding"], cursor=history["next_cursor"])
        self.assertEqual(older["items"][0]["temporal_status"], "historical_at_cut")
        self.assertIsNone(older["next_cursor"])
        empty = self._observe("decision_history", collection="checkpoints", page_size=1, source_binding=fixed)
        self.assertEqual(empty["state"], "empty")
        self.assertEqual(empty["completeness"], "page")
        self.assertIsNotNone(empty["next_cursor"])
        selected = self._observe("decision_history", collection="checkpoints", page_size=1, source_binding=fixed, cursor=empty["next_cursor"])
        self.assertEqual(selected["items"][0]["after"]["checkpoint_id"], first["result"]["checkpoint_id"])
        self.assertEqual(selected["items"][0]["temporal_status"], "current_at_cut")

    def test_exact_source_pages_preserve_revision_scope_bytes_and_denials(self):
        epoch, text, binary, capture = self._prepare_exact_retrieval_fixture()
        scopes = (CaptureScope(capture.capture_id, 1, {"description": "  binary finite case only\n", "excluded": "uniform statement"}),
                  CaptureScope(capture.capture_id, 0, {"description": "δ😀 exact text"}))
        first = self._source_evidence(epoch, "evidence.observation", sources=scopes)
        first_handle = f"evidence:{first.evidence_id}@1"
        with mock.patch.object(self.store, "read_raw_capture_artifact_bytes", side_effect=AssertionError("navigation loaded raw bytes")):
            read = self._observe("exact_record", handle=first_handle)
        encoded_document = base64.b64decode(read["items"][0]["content_base64"])
        document = json.loads(encoded_document)
        self.assertIsNone(read["items"][0]["next_offset_bytes"])
        self.assertEqual(read["items"][0]["sha256"], hashlib.sha256(encoded_document).hexdigest())
        self.assertEqual(document["source_navigation"]["items"][0]["exact_scope"], deep_thaw(scopes[0].exact_scope))
        self._source_evidence(epoch, first.evidence_id, sources=(scopes[1],), previous=first)
        before = deep_thaw(self.store.read_metadata())

        @contextlib.contextmanager
        def selected_blob_io(paths):
            # Count both dependency verification's Path streams and the exact
            # raw snapshot's descriptor stream; neither is physical disk IO.
            original_open, original_fdopen = os.open, os.fdopen
            original_path_open = Path.open
            descriptors = {}
            counts = {path: {"opens": 0, "bytes": 0, "max_read": 0} for path in paths}

            class MeteredStream:
                def __init__(self, stream, path):
                    self.stream = stream
                    self.count = counts[path]

                def __enter__(self):
                    self.stream.__enter__()
                    return self

                def __exit__(self, *arguments):
                    return self.stream.__exit__(*arguments)

                def __getattr__(self, name):
                    return getattr(self.stream, name)

                def read(self, *arguments, **keywords):
                    raw = self.stream.read(*arguments, **keywords)
                    requested = arguments[0] if arguments else keywords.get("size", -1)
                    self.count["max_read"] = max(self.count["max_read"], requested)
                    self.count["bytes"] += len(raw)
                    return raw

            def path_open(selected, *arguments, **keywords):
                stream = original_path_open(selected, *arguments, **keywords)
                if selected in counts:
                    counts[selected]["opens"] += 1
                    return MeteredStream(stream, selected)
                return stream

            def descriptor_open(selected, *arguments, **keywords):
                descriptor = original_open(selected, *arguments, **keywords)
                if Path(selected) in counts:
                    counts[Path(selected)]["opens"] += 1
                    descriptors[descriptor] = Path(selected)
                return descriptor

            def fdopen(descriptor, *arguments, **keywords):
                stream = original_fdopen(descriptor, *arguments, **keywords)
                if descriptor in descriptors:
                    return MeteredStream(stream, descriptors.pop(descriptor))
                return stream

            with mock.patch.object(Path, "open", path_open), mock.patch.object(
                os, "open", descriptor_open,
            ), mock.patch.object(os, "fdopen", fdopen):
                yield counts

        paths = {self.cas.path_for_digest(hashlib.sha256(raw).hexdigest()): raw for raw in (binary, text)}
        for ordinal, expected in ((0, binary), (1, text)):
            handle = document["source_navigation"]["items"][ordinal]["handle"]
            selected_path = self.cas.path_for_digest(hashlib.sha256(expected).hexdigest())
            offset, pieces = 0, []
            while offset is not None:
                # Isolate the projection's hashing from the Store/CAS hashing
                # that must still authenticate the exact returned raw bytes.
                with mock.patch.object(mission_observation, "hashlib", wraps=hashlib) as projection_hashes, selected_blob_io(paths) as physical:
                    result = self._observe("exact_record", handle=handle, source_evidence=first_handle, source_ordinal=ordinal,
                                           source_binding=read["source_binding"], offset_bytes=offset, max_bytes=7)
                for path, expected_bytes in paths.items():
                    passes = 1 if path == selected_path else 2
                    self.assertEqual(physical[path]["opens"], passes)
                    self.assertEqual(physical[path]["bytes"], passes * len(expected_bytes))
                    self.assertLessEqual(physical[path]["max_read"], 1024 * 1024)
                page = result["items"][0]
                chunk = base64.b64decode(page["content_base64"], validate=True)
                projection_hashes.sha256.assert_called_once_with(chunk)
                self.assertEqual(hashlib.sha256(chunk).hexdigest(), page["page_sha256"])
                self.assertEqual(page["sha256"], hashlib.sha256(expected).hexdigest())
                self.assertEqual(page["source_relation"]["exact_scope"], deep_thaw(scopes[ordinal].exact_scope))
                self.assertEqual(page["source_relation"]["source_ordinal"], ordinal)
                self.assertEqual(page["source_relation"]["evidence_reference"]["revision"], 1)
                self.assertEqual(page["trust_class"], "untrusted_raw_material")
                pieces.append(chunk)
                offset = page["next_offset_bytes"]
            self.assertEqual(b"".join(pieces), expected)
            descriptor, full = self.store.read_raw_capture_artifact_bytes(
                cas=self.cas, mission_id="mission.1", capture_id=capture.capture_id,
                artifact_ordinal=1 if ordinal == 0 else 0,
            )
            self.assertEqual(full, expected)
            self.assertEqual(descriptor["blob_sha256"], hashlib.sha256(expected).hexdigest())
        artifact = document["source_navigation"]["items"][0]["handle"]
        self.assertEqual(self._observe("exact_record", handle=artifact)["state"], "denied")
        self.assertEqual(self._observe("exact_record", handle=artifact, source_evidence=f"evidence:{first.evidence_id}@2", source_ordinal=0)["state"], "denied")
        for handle, ordinal in ((artifact, 99), (artifact + "@1", 0), ("capture-artifact:malformed", 0)):
            denied = self._observe("exact_record", handle=handle, source_evidence=first_handle, source_ordinal=ordinal)
            self.assertEqual((denied["state"], denied["reason"]), ("denied", "artifact_not_in_selected_evidence_relation"))
        end = self._observe("exact_record", handle=artifact, source_evidence=first_handle,
                            source_ordinal=0, offset_bytes=len(binary))
        self.assertEqual(end["items"][0]["content_base64"], "")
        self.assertIsNone(end["items"][0]["next_offset_bytes"])
        with self.assertRaisesRegex(ValueError, "exceeds exact extent"):
            self._observe("exact_record", handle=artifact, source_evidence=first_handle,
                          source_ordinal=0, offset_bytes=len(binary) + 1)
        for handle in ("session:private@1", "capture-annotation:private@1", f"capture:{capture.capture_id}"):
            self.assertEqual(self._observe("exact_record", handle=handle)["state"], "denied")
        with mock.patch.object(self.store, "_raw_capture_artifact_read_authority", side_effect=RawCaptureArtifactUnavailableError(
            capture_id=capture.capture_id, artifact_ordinal=1, disposition="quarantined")):
            unavailable = self._observe("exact_record", handle=artifact, source_evidence=first_handle, source_ordinal=0)
            self.assertEqual(unavailable["state"], "unavailable")
        with mock.patch.object(self.store, "_raw_capture_artifact_read_authority", side_effect=WorkspaceIntegrityError("fixture integrity failure")):
            with self.assertRaises(WorkspaceIntegrityError):
                self._observe("exact_record", handle=artifact, source_evidence=first_handle, source_ordinal=0)
        self.assertEqual(deep_thaw(self.store.read_metadata()), before)

    def test_exact_source_page_rechecks_related_blobs_and_current_security(self):
        epoch, text, binary, capture = self._prepare_exact_retrieval_fixture()
        evidence = self._source_evidence(epoch, "evidence.page-dependencies", sources=(
            CaptureScope(capture.capture_id, 0, "The selected text."),
            CaptureScope(capture.capture_id, 1, "The independently required binary."),
        ))
        selection = {
            "handle": f"capture-artifact:{capture.capture_id}#0",
            "source_evidence": f"evidence:{evidence.evidence_id}@1", "source_ordinal": 0,
            "offset_bytes": 0, "max_bytes": 1,
            "source_binding": self._observe("current_decision")["source_binding"],
        }
        before = deep_thaw(self.store.read_metadata())
        self.assertEqual(self._observe("exact_record", **selection)["state"], "present")
        for raw in (text, binary):
            path = self.cas.path_for_digest(hashlib.sha256(raw).hexdigest())
            with self.subTest(corrupt_selected=raw == text):
                path.chmod(0o600)
                try:
                    with path.open("r+b") as stream:
                        stream.seek(len(raw) - 1)
                        stream.write(b"!")
                    rejected = self._observe("exact_record", **selection)
                    self.assertEqual((rejected["state"], rejected["reason"]),
                                     ("unavailable", "selected_record_not_retained"))
                    self.assertEqual(rejected["items"], [])
                    # Missing/mismatched relation remains subordinate to the
                    # same Evidence dependency failure as the ordinary reader.
                    rejected = self._observe("exact_record", **{**selection, "source_ordinal": 99})
                    self.assertEqual(rejected["state"], "unavailable")
                    with self.assertRaises(StaleCommandError):
                        self.store.read_evidence_meaning_revision(evidence.evidence_id, 1)
                finally:
                    path.write_bytes(raw)

        # Inject a newly observed disposition at the existing security-record
        # boundary. The real enforcement runs; this does not prove a writer or
        # invent current ordinary Evidence Blob-role publication authority.
        for blobs, references in (
            ((hashlib.sha256(text).hexdigest(),), ()),
            ((hashlib.sha256(binary).hexdigest(),), ()),
            ((), ((evidence.evidence_id, 1),)),
        ):
            with self.subTest(disposed_blobs=blobs, disposed_evidence=references):
                directive = DeletionDirective(
                    directive_id="directive.page-test", authorization_sha256="a" * 64,
                    reason="Exact fixture security disposition.",
                    blob_sha256s=blobs, evidence_references=references,
                )
                self.assertEqual(self._observe("exact_record", **selection)["state"], "present")
                with mock.patch.object(workspace_store, "_closure_security_records", return_value=((directive,), ())):
                    rejected = self._observe("exact_record", **selection)
                    self.assertEqual((rejected["state"], rejected["reason"]),
                                     ("unavailable", "selected_record_not_retained"))
                    self.assertEqual(rejected["items"], [])
                    with self.assertRaisesRegex(StaleCommandError, "security-disposed"):
                        self.store.read_evidence_meaning_revision(evidence.evidence_id, 1)
        self.assertEqual(deep_thaw(self.store.read_metadata()), before)

    def test_source_page_physical_selection_does_not_skip_other_blob_roles(self):
        _epoch, text, binary, _capture = self._prepare_exact_retrieval_fixture()
        blobs = sorted((hashlib.sha256(raw).hexdigest(), raw) for raw in (text, binary))
        selected_digest, selected_raw = blobs[0]
        role_digest, role_raw = blobs[1]
        selection = workspace_store._SelectedBlobRange(selected_digest, 0, 1)
        # The shared physical boundary receives the source/role union, not
        # source provenance. Current ordinary Evidence writers forbid private
        # roles; this oracle preserves the retained-role consumer boundary.
        with self.store.direct_recovery_read_scope(), self.store._connection(read_only=True) as connection:
            verified, page = workspace_store._require_selected_blob_physical_availability(
                connection, cas=self.cas, blob_sha256s=(selected_digest, role_digest),
                label="source plus retained role fixture", selected_range=selection,
            )
            self.assertEqual((verified.sha256, page), (selected_digest, selected_raw[:1]))
            role_path = self.cas.path_for_digest(role_digest)
            role_path.chmod(0o600)
            try:
                role_path.write_bytes(b"!" * len(role_raw))
                # The selected prefix was already collected; a later role
                # failure must prevent any page/result from escaping.
                with self.assertRaises(StaleCommandError):
                    workspace_store._require_selected_blob_physical_availability(
                        connection, cas=self.cas, blob_sha256s=(selected_digest, role_digest),
                        label="source plus retained role fixture", selected_range=selection,
                    )
            finally:
                role_path.write_bytes(role_raw)

    def test_exact_source_page_requires_indexed_digest_and_snapshot(self):
        epoch, _text, _binary, capture = self._prepare_exact_retrieval_fixture()
        evidence = self._source_evidence(epoch, "evidence.page-authority", sources=(
            CaptureScope(capture.capture_id, 0, "The exact selected source."),
        ))
        options = {
            "mission_id": "mission.1", "evidence_id": evidence.evidence_id, "revision": 1,
            "expected_payload_digest": evidence.payload_digest,
            "source_project_commit": int(self.store.read_metadata()["current_project_commit"]),
            "source_ordinal": 0, "artifact_handle": f"capture-artifact:{capture.capture_id}#0",
            "offset_bytes": 0, "max_bytes": 1,
        }
        with self.assertRaisesRegex(WorkspaceIntegrityError, "held read snapshot"):
            self.store.read_evidence_source_artifact_page(**options)
        with self.store.direct_recovery_read_scope():
            with self.assertRaisesRegex(WorkspaceIntegrityError, "authenticated payload"):
                self.store.read_evidence_source_artifact_page(**{**options, "expected_payload_digest": "0" * 64})
            self.assertIsNotNone(self.store.read_evidence_source_artifact_page(**options))

    def test_output_catalog_scans_one_bounded_page_and_response_extent_is_explicit(self):
        from dataclasses import replace
        from research_core import owner_content_access
        from research_core.mission_evidence import (
            RawCaptureArtifactInput, commit_raw_capture, prepare_raw_capture,
        )

        # None is an exact Epoch selection, not a wildcard or an empty string.
        # Before the first Epoch there can still be authentic unbound custody.
        self.assertEqual(self._observe("catalog", collection="latest_outputs")["state"], "empty")
        null_output = None
        for kind in ("assignment", "output"):
            capture = prepare_raw_capture(self.store, mission_id="mission.1", executive_epoch_id=None,
                capture_kind=kind, observation_id=f"observation.null-epoch.{kind}",
                assignment_id="observation.null-epoch", provenance={"kind": "observation_fixture"},
                artifacts=(RawCaptureArtifactInput(role="text_result", logical_name="unbound.txt",
                    content_bytes=b"Unbound synthetic observation.", media_type="text/plain", encoding="utf-8"),))
            commit_raw_capture(self.store, cas=self.cas, record=capture,
                lease=self.interface._writer_lease(), actor="test.observation.null-epoch")
            if kind == "output":
                null_output = f"capture:{capture.capture_id}"
        with mock.patch.object(self.store, "read_owner_content_inventory_page",
                side_effect=AssertionError("latest output must not scan Mission-wide inventory")):
            unbound = self._observe("catalog", collection="latest_outputs", page_size=1)
        self.assertEqual([row["value"]["handle"] for row in unbound["items"]], [null_output])
        self.assertIsNone(unbound["items"][0]["value"]["executive_epoch_id"])
        self.assertEqual(unbound["completeness"], "exhausted")
        self.assertIsNone(unbound["next_cursor"])
        with self.assertRaisesRegex(WorkspaceIntegrityError, "direct_recovery_read_scope"):
            self.store.read_observation_capture_page(mission_id="mission.1",
                source_project_commit=unbound["source_binding"]["project_commit"],
                origin_epoch_id=None, capture_kind="output")
        with self.store.direct_recovery_read_scope():
            cut = int(self.store.read_metadata()["current_project_commit"])
            missing = self.store.read_observation_capture_page(mission_id="mission.1", source_project_commit=cut,
                origin_epoch_id="epoch.not-present", capture_kind="output", page_size=1)
            self.assertEqual(deep_thaw(missing), {"records": [], "next_after": None})
            for changed in ({"origin_epoch_id": ""}, {"origin_epoch_id": False}, {"capture_kind": "unknown"},
                            {"page_size": True}, {"after": ("capture", "x", True)}):
                with self.subTest(invalid_selection=changed), self.assertRaises(ValueError):
                    self.store.read_observation_capture_page(**{
                        "mission_id": "mission.1", "source_project_commit": cut,
                        "origin_epoch_id": None, "capture_kind": "output", "page_size": 1, **changed})
            # Inject a broken positive join after real authenticated membership
            # traversal. It must fail, not masquerade as negative exhaustion.
            real_index = owner_content_access._index
            for fault in (None, "missing", "epoch", "kind"):
                observed_indexes = []

                def broken_index(*args, **kwargs):
                    actual = real_index(*args, **kwargs)
                    observed_indexes.append(actual)
                    proxy = mock.Mock(wraps=actual)

                    def posting(directory, key):
                        # Membership traversal has closed its generator, but
                        # its authenticated page cache must survive the joins.
                        self.assertEqual(actual._engine._depth, 1)
                        self.assertTrue(actual._engine._cache)
                        found = actual.posting(directory, key)
                        self.assertIsNotNone(found)
                        if fault is None:
                            return found
                        if fault == "missing":
                            return None
                        metadata = deep_thaw(found.metadata)
                        metadata["capture_descriptor"]["origin_epoch_id" if fault == "epoch" else "capture_kind"] = (
                            "epoch.wrong" if fault == "epoch" else "assignment")
                        return replace(found, metadata=metadata)

                    proxy.posting.side_effect = posting
                    return proxy

                with self.subTest(positive_join_fault=fault), mock.patch.object(
                        owner_content_access, "_index", side_effect=broken_index):
                    with (contextlib.nullcontext() if fault is None else self.assertRaisesRegex(
                            WorkspaceIntegrityError, "exact posting|typed membership")):
                        result = self.store.read_observation_capture_page(mission_id="mission.1", source_project_commit=cut,
                            origin_epoch_id=None, capture_kind="output", page_size=1)
                    if fault is None:
                        self.assertEqual([row["descriptor"]["id"] for row in result["records"]], [null_output])
                    self.assertEqual(len(observed_indexes), 1)
                    engine = observed_indexes[0]._engine
                    self.assertEqual((engine._depth, engine._cache_bytes), (0, 0))
                    self.assertFalse(engine._cache)
                    self.assertFalse(engine._pending)

        epoch = self._authorize()
        self._bind(epoch)
        retrieval_fixtures.RootRetrievalTests._capture(self, epoch, "observation-assignment", kind="assignment")
        output_id = retrieval_fixtures.RootRetrievalTests._capture(self, epoch, "observation-output")
        other_output_id = retrieval_fixtures.RootRetrievalTests._capture(self, epoch, "observation-other-output")
        expected_outputs = sorted((output_id, other_output_id))
        with mock.patch.object(self.store, "read_observation_capture_page", wraps=self.store.read_observation_capture_page) as pages, \
                mock.patch.object(self.store, "read_owner_content_inventory_page",
                    side_effect=AssertionError("latest output must not scan Mission-wide inventory")):
            def bounded_page(**selection):
                calls_before = pages.call_count
                result = self._observe("catalog", collection="latest_outputs", page_size=1, **selection)
                self.assertEqual(pages.call_count, calls_before + 1)
                self.assertEqual(pages.call_args.kwargs["page_size"], 1)
                self.assertEqual(pages.call_args.kwargs["capture_kind"], "output")
                self.assertEqual(pages.call_args.kwargs["source_project_commit"], result["source_binding"]["project_commit"])
                self.assertEqual(result["completeness"], "page" if result["next_cursor"] is not None else "exhausted")
                self.assertEqual(result["coverage"]["ordering"], "ascending_capture_identity")
                self.assertEqual(result["coverage"]["scope"], "selected_epoch_captured_outputs")
                self.assertLessEqual(len(result["items"]), 1)
                return result

            first = bounded_page()
            self.assertEqual(first["state"], "present")
            self.assertEqual(first["items"][0]["value"]["handle"], expected_outputs[0])
            self.assertEqual(pages.call_args.kwargs["origin_epoch_id"], epoch)
            self.assertIsNotNone(first["next_cursor"])
            fixed = first["source_binding"]

            # A genuine successor changes both the latest Epoch and the old
            # Captures' current descriptors, but cannot splice into this cursor.
            checkpoint = self._execute(CHECKPOINT, {}, executive_epoch_id=epoch)
            self.assertEqual(checkpoint["status"], "completed", checkpoint)
            self.goal_thread_id = "goal-thread:observation-output-successor"
            successor = self._authorize()
            self._bind(successor)
            self.assertNotEqual(successor, epoch)
            successor_output = retrieval_fixtures.RootRetrievalTests._capture(self, successor, "observation-successor-output")
            before_reads = deep_thaw(self.store.read_metadata())

            rows = list(first["items"])
            page = first
            for _ in range(1):  # Only the second selected output remains.
                if page["next_cursor"] is None:
                    break
                page = bounded_page(source_binding=fixed, cursor=page["next_cursor"])
                self.assertEqual(page["source_binding"], fixed)
                self.assertEqual(pages.call_args.kwargs["origin_epoch_id"], epoch)
                rows.extend(page["items"])
            self.assertIsNone(page["next_cursor"])
            self.assertEqual([row["value"]["handle"] for row in rows], expected_outputs)
            self.assertTrue(all(row["value"]["executive_epoch_id"] == epoch and
                                row["value"]["capture_kind"] == "output" for row in rows))

            calls_before = pages.call_count
            stale_first = self._observe("catalog", collection="latest_outputs", page_size=1, source_binding=fixed)
            self.assertEqual(stale_first["state"], "unavailable")
            self.assertEqual(stale_first["reason"], "output_catalog_first_page_requires_current_cut")
            self.assertEqual(stale_first["items"], [])
            self.assertEqual(stale_first["source_binding"], fixed)
            self.assertEqual(pages.call_count, calls_before)

            fresh = bounded_page()
            self.assertGreater(fresh["source_binding"]["project_commit"], fixed["project_commit"])
            self.assertEqual(pages.call_args.kwargs["origin_epoch_id"], successor)
            self.assertIsNone(fresh["next_cursor"])
            rows = list(fresh["items"])
            self.assertEqual([row["value"]["handle"] for row in rows], [successor_output])
            self.assertEqual(rows[0]["value"]["executive_epoch_id"], successor)
            self.assertEqual(rows[0]["value"]["capture_kind"], "output")
            self.assertEqual(deep_thaw(self.store.read_metadata()), before_reads)
        with mock.patch.object(mission_observation, "_current", return_value={"oversized": "x" * (1024 * 1024)}):
            result = self._observe("current_decision")
            self.assertEqual(result["reason"], "observation_response_extent_exceeded")
            self.assertEqual(result["items"], [])


class MissionObservationMigrationTests(unittest.TestCase):
    def test_migrated_root5_decisions_and_exact_evidence_sources_use_authenticated_index(self):
        from contextlib import ExitStack

        from research_core import mission_frontier, mission_retrieval
        from research_core import workspace_store as store_module
        from research_core.mission_interface import _formal_request_projection
        from research_core.mission_evidence import (
            RawCaptureArtifactInput,
            commit_evidence_meaning,
            commit_raw_capture,
            prepare_evidence_meaning,
            prepare_raw_capture,
        )
        from research_core.workspace_recovery import (
            _remove_tree,
            import_portable_backup_to_staging,
        )
        from test_workspace_schema10_migration import Schema10MigrationTests
        from test_workspace_schema12_migration import Schema12MigrationTests

        # The established BackupSet fixture supplies its required persisted
        # closure. The ordinary scientific records exercised below are distinct
        # from that reserved recovery/A1 material and predate both migrations.
        migration = Schema12MigrationTests(methodName="runTest")
        self.addCleanup(migration.doCleanups)
        source = migration._fixture(retained_root5=True)
        self.fixture = source
        old_strategy = source.store.get_head(store_module.TypedWorkspaceId(
            store_module.IdentityKind.STRATEGY, "strategy.recovery",
        ))
        self.assertIsNotNone(old_strategy)
        original_strategy = deep_thaw(source.store.read_strategy_revision(
            mission_id=source.mission_id, strategy_id="strategy.recovery", revision=1,
        ))
        original_typed_strategy = mission_frontier._stored_strategy(original_strategy)
        expected_strategy_document = mission_retrieval._semantic_document(
            "strategy", original_typed_strategy.record.document,
        )
        expected_strategy_document["formal_requests"] = _formal_request_projection(
            original_typed_strategy,
        )
        new_strategy = Schema10MigrationTests._advance_strategy_revision(
            self, source.store, command_id="observation.schema9.strategy",
            marker="retained-observation-decision",
        )
        self.assertIsNotNone(new_strategy)
        metadata = source.store.read_metadata()
        lease = source.store.claim_writer(
            owner="test.observation.schema9-source",
            creation_basis="retain ordinary observation source records",
            expected_project_commit=int(metadata["current_project_commit"]),
            expected_root_digest=str(metadata["current_root_digest"]),
            expected_canonical_authority_digest=source.authority_digest,
        )
        exact_text = "retained root5 source δ😀\n".encode("utf-8")
        exact_binary = b"\x00\xffretained-root5\x00\xfe"
        try:
            capture = prepare_raw_capture(
                source.store, mission_id=source.mission_id,
                executive_epoch_id=source.epoch_id, capture_kind="output",
                observation_id="observation.schema9-source",
                assignment_id="observation.schema9-source",
                provenance={"kind": "migrated_observation_fixture"},
                completion={"lifecycle": "completed"},
                artifacts=(
                    RawCaptureArtifactInput(role="text_result", logical_name="retained.txt",
                        content_bytes=exact_text, media_type="text/plain", encoding="utf-8"),
                    RawCaptureArtifactInput(role="binary_result", logical_name="retained.bin",
                        content_bytes=exact_binary),
                ),
            )
            commit_raw_capture(source.store, cas=source.cas, record=capture,
                lease=lease, actor="test.observation.schema9-source")
            scopes = (
                CaptureScope(capture.capture_id, 1,
                    {"description": "  finite binary case only\n", "excluded": "uniform claim"}),
                CaptureScope(capture.capture_id, 0,
                    {"description": "δ😀 exact retained text", "lines": [1, 1]}),
            )
            evidence = None
            for selected_scopes in (scopes, (scopes[1],)):
                record = prepare_evidence_meaning(
                    authority=source.epoch_authority,
                    evidence_id="evidence.observation.retained-root5",
                    statement="Exact retained source fixture; no mathematical theorem.",
                    exact_scope="Synthetic retained artifacts only.",
                    strength="conditional", semantic_role="result",
                    authority_basis="Interpretation of the exact synthetic artifacts.",
                    sources=selected_scopes, non_inferences=("No RH conclusion follows.",),
                    expected_head_revision=None if evidence is None else evidence.revision,
                    expected_head_payload_digest=None if evidence is None else evidence.payload_digest,
                )
                commit_evidence_meaning(source.store, record=record,
                    lease=lease, actor="test.observation.schema9-source")
                evidence = record
        finally:
            source.release_writer(lease)
        self.assertEqual(source.store.read_metadata()["schema_version"], 9)
        backup = source.create_backup("observation-retained-root5-source")
        root6 = migration._migrate(backup, target=10, label="observation-root6")
        indexed = migration._migrate(root6, target=12, label="observation-indexed-root6")
        self.assertEqual(indexed.manifest.store.schema_version, 12)
        self.assertEqual(indexed.manifest.store.root_digest_version, 6)

        # Use the existing staging import owner to rebind the sealed BackupSet
        # into a disposable WAL workspace. Do not open/unseal the artifact as
        # an ordinary Store or mutate historical schema/transition witnesses.
        release_root, release_sha = source.installed_release_fixture()
        target_parent = source.root / "rh-staging" / "workspaces"
        target_parent.mkdir(parents=True)
        target = (target_parent / source.mission_id).resolve(strict=False)
        self.addCleanup(_remove_tree, target, owned_parent=target_parent, ignore_errors=True)
        imported = import_portable_backup_to_staging(
            backup_directory=indexed.path, target_root=target,
            installed_release_root=release_root, installed_release_sha=release_sha,
            mission_id=source.mission_id, expected_backup_id=indexed.manifest.backup_id,
            expected_manifest_sha256=indexed.manifest.manifest_sha256,
        )
        self.assertEqual(imported.status, "staging_imported_quiesced")
        interface = fixtures.MissionInterface.open(target,
            expected_project_id=source.project_id, expected_mission_id=source.mission_id,
            canonical_snapshot=source.snapshot)
        store = interface._store
        before = deep_thaw(store.read_metadata())
        self.assertEqual(before["schema_version"], 12)
        self.assertEqual(before["root_digest_version"], 6)
        self.assertIsNone(before["current_writer_epoch"])
        with store.direct_recovery_read_scope():
            self.assertEqual(store.read_root_retrieval_cut()["route"],
                "migrated_root5_exhaustive_compatibility")
            prebootstrap_binding = mission_observation._binding(interface,
                store.read_observation_cut(
                    cut_project_commit=root6.manifest.store.project_commit_id))

        validator = Draft7Validator(json.loads(SCHEMA_PATH.read_text(encoding="utf-8")))
        key = "be" * 32

        def observe(operation, *, expected_state="present", **selection):
            result = deep_thaw(interface.observe_mission(
                {"operation": operation, **selection}, cursor_mac_key=key))
            validator.validate(result)
            self.assertNotIn(key, json.dumps(result))
            self.assertEqual(result["state"], expected_state, result)
            return result

        # Only the actual read interval is guarded: migration/backup/full audit
        # may legitimately enumerate the source once while constructing the
        # authenticated index. Observation must never rediscover that history.
        with ExitStack() as guards:
            for name in ("_validated_journal_index", "_validated_transition_journal_chain",
                         "_direct_recovery_facts_from_connection",
                         "_require_historical_typed_revision_origin"):
                guards.enter_context(mock.patch.object(store_module, name,
                    side_effect=AssertionError(f"observation entered legacy history route: {name}")))
            guards.enter_context(mock.patch.object(store, "claim_writer",
                side_effect=AssertionError("observation acquired a writer")))
            guards.enter_context(mock.patch.object(interface, "reconstruct",
                side_effect=AssertionError("observation reconstructed all owners")))
            current = observe("current_decision")
            binding = current["source_binding"]
            self.assertEqual(binding["project_commit"], before["current_project_commit"])
            decision = current["items"][0]
            self.assertEqual(decision["mission"]["handle"], f"mission:{source.mission_id}@1")
            self.assertEqual(decision["strategy"]["handle"], "strategy:strategy.recovery@2")
            self.assertEqual(decision["strategy"]["semantic"]["integrated_comparison"],
                new_strategy.payload["integrated_comparison"])
            with mock.patch.object(store, "read_raw_capture_artifact_bytes",
                    side_effect=AssertionError("catalog loaded raw artifact bytes")):
                outputs = observe("catalog", collection="latest_outputs", page_size=50,
                    source_binding=binding)
                owners = observe("catalog", collection="owner_records", page_size=50,
                    source_binding=binding)
            selected_outputs = [item["value"] for item in outputs["items"]
                if item["value"]["handle"] == f"capture:{capture.capture_id}"]
            self.assertEqual(len(selected_outputs), 1)
            self.assertEqual(selected_outputs[0]["executive_epoch_id"], source.epoch_id)
            self.assertEqual(selected_outputs[0]["artifact_count"], 2)
            self.assertEqual(outputs["coverage"]["scope"], "selected_epoch_captured_outputs")
            self.assertIsNone(outputs["coverage"]["source_inventory_count"])
            owner_handles = {item["value"]["handle"] for item in owners["items"]}
            self.assertTrue({f"mission:{source.mission_id}@1", "strategy:strategy.recovery@2"}
                <= owner_handles)
            unavailable = observe("current_decision", source_binding=prebootstrap_binding,
                expected_state="unavailable")
            self.assertEqual(unavailable["reason"], "owner_content_index_not_introduced_at_cut")
            self.assertEqual(unavailable["items"], [])
            first = observe("decision_history", collection="strategies", page_size=1,
                source_binding=binding)
            self.assertIsNotNone(first["next_cursor"])
            second = observe("decision_history", collection="strategies", page_size=1,
                source_binding=binding, cursor=first["next_cursor"])
            self.assertIsNone(second["next_cursor"])
            self.assertEqual(first["items"][0]["after"]["revision"], 1)
            self.assertEqual(second["items"][0]["after"]["revision"], 2)
            self.assertEqual(second["items"][0]["before"], first["items"][0]["after"])
            self.assertEqual(first["items"][0]["temporal_status"], "historical_at_cut")
            self.assertEqual(second["items"][0]["temporal_status"], "current_at_cut")
            old = observe("exact_record", handle="strategy:strategy.recovery@1", source_binding=binding)
            self.assertEqual(json.loads(base64.b64decode(old["items"][0]["content_base64"]))[
                "integrated_comparison"], old_strategy.payload["integrated_comparison"])

            # Exercise the real root dispatcher without activating the imported
            # Mission: its independent live Epoch gate is covered by root-query
            # tests. This interval proves exact historical Strategy projection
            # uses the same authenticated cut/index as observation, not the
            # exhaustive legacy-origin reader guarded above.
            strategy_handle = "strategy:strategy.recovery@1"
            selection = {"mode": "read", "purpose": "retained Strategy decision",
                         "ids": [strategy_handle]}
            with mock.patch.object(interface, "_direct_epoch_authority", return_value=object()), \
                    mock.patch.object(mission_frontier, "_stored_strategy",
                        wraps=mission_frontier._stored_strategy) as stored_strategy:
                exact = mission_retrieval.root_retrieve(interface, selection,
                    executive_epoch_id=source.epoch_id)["items"][0]
            self.assertEqual(exact["status"], "readable")
            self.assertEqual(exact["id"], strategy_handle)
            self.assertEqual(json.loads(exact["readable_content"]), expected_strategy_document)
            self.assertEqual(deep_thaw(stored_strategy.call_args.args[0]), original_strategy)
            with store.direct_recovery_read_scope():
                cut = int(store.read_root_retrieval_cut()["project_commit"])
                with mock.patch.object(interface, "_mission_id", "mission.other"):
                    absent = mission_retrieval._exact_read(interface, strategy_handle,
                        source_project_commit=cut)
                self.assertEqual(absent["status"], "not_found")
                for failure in (store_module.OwnerContentIndexUnavailableError,
                                store_module.WorkspaceIntegrityError):
                    with self.subTest(index_failure=failure.__name__), \
                            mock.patch.object(store, "read_indexed_observation_owner",
                                side_effect=failure("unavailable or inconsistent index")), \
                            self.assertRaises(failure):
                        mission_retrieval._exact_read(interface, strategy_handle,
                            source_project_commit=cut)

            old_handle = "evidence:evidence.observation.retained-root5@1"
            with mock.patch.object(store, "read_raw_capture_artifact_bytes",
                    side_effect=AssertionError("Evidence navigation loaded raw bytes")):
                old = observe("exact_record", handle=old_handle, source_binding=binding)
                newest = observe("exact_record", handle="evidence:evidence.observation.retained-root5@2",
                    source_binding=binding)
            old_document = json.loads(base64.b64decode(old["items"][0]["content_base64"]))
            new_document = json.loads(base64.b64decode(newest["items"][0]["content_base64"]))
            expected = [{"ordinal": ordinal,
                "handle": f"capture-artifact:{scope.capture_id}#{scope.artifact_ordinal}",
                "exact_scope": deep_thaw(scope.exact_scope)} for ordinal, scope in enumerate(scopes)]
            self.assertEqual(old_document["source_navigation"]["items"], expected)
            self.assertEqual(new_document["source_navigation"]["items"], [{**expected[1], "ordinal": 0}])
            for ordinal, raw in ((0, exact_binary), (1, exact_text)):
                artifact = observe("exact_record", handle=expected[ordinal]["handle"],
                    source_evidence=old_handle, source_ordinal=ordinal, source_binding=binding)
                page = artifact["items"][0]
                self.assertEqual(base64.b64decode(page["content_base64"], validate=True), raw)
                self.assertEqual(page["sha256"], hashlib.sha256(raw).hexdigest())
                self.assertEqual(page["source_relation"]["exact_scope"], expected[ordinal]["exact_scope"])
                self.assertEqual(page["trust_class"], "untrusted_raw_material")
        self.assertEqual(deep_thaw(store.read_metadata()), before)


if __name__ == "__main__":
    unittest.main()
