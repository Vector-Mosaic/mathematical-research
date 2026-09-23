from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import sqlite3
import sys
import time
import unittest
from contextlib import ExitStack, closing
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

_ROOT6_PUBLIC_RETRIEVAL_SCALE_FLAG = (
    "--root6-public-retrieval-scale-qualification"
)
_ROOT6_PUBLIC_RETRIEVAL_SCALE_CLI_OPT_IN = (
    __name__ == "__main__"
    and _ROOT6_PUBLIC_RETRIEVAL_SCALE_FLAG in sys.argv[1:]
)
_ROOT6_PUBLIC_RETRIEVAL_SCALE_ENABLED = (
    _ROOT6_PUBLIC_RETRIEVAL_SCALE_CLI_OPT_IN
    or os.environ.get("RH_ROOT6_PUBLIC_RETRIEVAL_SCALE_QUALIFICATION") == "1"
)

PACKAGE_ROOT = Path(__file__).resolve().parents[1]
TEST_ROOT = Path(__file__).resolve().parent
for path in (PACKAGE_ROOT, TEST_ROOT):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

import test_mission_interface_direct as fixtures
from research_core import mission_retrieval as retrieval_module
from research_core import workspace_store as store_module
from research_core.evidence_store import EvidenceCAS
from research_core.json_support import canonical_json_bytes
from research_core.mission_evidence import (
    CaptureScope,
    InterpretedOwnerReference,
    commit_capture_scope_annotation,
    commit_evidence_meaning,
    prepare_capture_scope_annotation,
    prepare_evidence_meaning,
    read_capture_scope_annotation,
    read_evidence_meaning,
)
from research_core.mission_interface import MissionInterface
from research_core.mission_operation_contract import (
    CHECKPOINT, INTERPRET_MATERIAL, ORIENT, RECORD_BRANCH, RECORD_CANDIDATE,
    RECORD_CONTEXT,
    RECORD_STRATEGY, RETRIEVE, ROOT_RETRIEVE_MODES,
    MissionOperationContractError, project_model_usage, validate_historical_read_request,
    validate_semantic_request, validate_semantic_result,
)
from research_core.research_model import deep_thaw
from research_core.workspace_store import (
    RawCaptureArtifactUnavailableError,
    StaleCommandError,
    WorkspaceIntegrityError,
    canonical_raw_capture_id,
)


class HistoricalRelationshipProjectionTests(unittest.TestCase):
    def test_candidate_selected_context_reference_remains_discoverable(self):
        document = {"supporting_refs": [{
            "kind": "context", "id": "scientific-context", "revision": 7,
            "digest_sha256": "ab" * 32,
            "selection": {"mode": "treatments", "treatment_ids": ["bridge"]},
        }]}
        edges = MissionInterface._historical_explicit_edges(
            {"id": "candidate:selection-test@1", "kind": "candidate"},
            ["owner_reference"], authoritative_document=document,
        )
        self.assertEqual(deep_thaw(edges), [{
            "relationship_kind": "owner_reference",
            "source_id": "candidate:selection-test@1",
            "target_id": "context:scientific-context@7",
            "source_path": "/supporting_refs/0",
        }])


class RootRetrievalTests(unittest.TestCase):
    setUpClass = classmethod(fixtures.DirectMissionInterfaceTests.setUpClass.__func__)
    setUp = fixtures.DirectMissionInterfaceTests.setUp
    _request = staticmethod(fixtures.DirectMissionInterfaceTests._request)
    _authorize = fixtures.DirectMissionInterfaceTests._authorize
    _bind = fixtures.DirectMissionInterfaceTests._bind
    _execute = fixtures.DirectMissionInterfaceTests._execute
    _prepare_exact_retrieval_fixture = fixtures.DirectMissionInterfaceTests._prepare_exact_retrieval_fixture
    _capture_pending_material = fixtures.DirectMissionInterfaceTests._capture_pending_material
    key = {"cursor_mac_key": "cd" * 32}

    def _start(self) -> str:
        epoch = self._authorize()
        self._bind(epoch)
        return epoch

    def _query(self, epoch: str, mode: str, *, service=None, **selection):
        response = (service or self.interface).execute_semantic_operation(
            self._request(RETRIEVE, {"mode": mode, "purpose": "Deliberate test selection.", **selection}),
            executive_epoch_id=epoch, root_query_context=self.key,
        )
        self.assertEqual(response["status"], "completed", response)
        validate_semantic_result(RETRIEVE, response)
        return deep_thaw(response["result"])

    def _capture(self, epoch: str, name: str, *, root: str | None = None, kind: str = "output") -> str:
        result = self.interface.capture_native_material_observation({
            "observationId": f"observation:{name}", "materialKind": kind,
            "content": f"Exact output for {name}. Quoted proof: {{\"closure\":\"δ😀\"}}",
            "rootThreadId": root or self.goal_thread_id, "parentThreadId": root or self.goal_thread_id,
            "childThreadId": f"child:{name}",
        }, executive_epoch_id=epoch)
        return str(result["material_id"])

    def _fail(self, epoch: str):
        result = self.interface.record_direct_failed_executive_epoch_from_owner({"executiveEpochId": epoch, "reconciliation": {"stage": "goal_runtime", "failure_reason": "goal_terminal_without_checkpoint"}})
        self.assertEqual(result["state"], "failed_before_checkpoint")

    def _successor(self, name: str) -> str:
        self.goal_thread_id = f"goal-thread:{name}"
        return self._start()

    def test_all_eight_modes_use_owner_dispatch_without_panorama_or_writes(self):
        epoch = self._start()
        before = deep_thaw(self.store.read_metadata())
        cases = {
            "read": {"ids": ["mission:mission.1", "strategy:strategy.theta.1"]},
            "search": {"query": "theta"}, "inventory": {},
            "checkpoint": {"section": "summary"}, "changes_since_checkpoint": {},
            "hooks": {}, "captures": {}, "proof_attention": {},
        }
        self.assertFalse(hasattr(self.interface, "_direct_frontier_panorama_items"))
        with mock.patch.object(self.interface, "_direct_frontier_panorama_items", side_effect=AssertionError("old panorama called"), create=True), mock.patch.object(self.interface, "reconstruct", side_effect=AssertionError("reconstruction called")):
            for mode, selection in cases.items():
                with self.subTest(mode=mode):
                    result = self._query(epoch, mode, **selection)
                    self.assertEqual(result["mode"], mode)
                    self.assertIsNone(result["next_cursor"])
                    if mode in {"checkpoint", "changes_since_checkpoint"}:
                        self.assertEqual(result["state"], "no_checkpoint")
            oriented = self.interface.execute_semantic_operation(self._request(ORIENT, {}), executive_epoch_id=epoch)
            self.assertEqual(oriented["status"], "completed", oriented)
            self.assertEqual(set(oriented["result"]), {"orientation"})
        self.assertEqual(before, deep_thaw(self.store.read_metadata()))

    def test_inventory_continues_across_fresh_facade_and_interleaved_capture_commits(self):
        epoch = self._start()
        expected = self._query(epoch, "inventory")["items"]
        result = self._query(epoch, "inventory", page_size=1)
        observed = list(result["items"])
        count = 0
        while result["next_cursor"] is not None:
            count += 1
            self._capture(epoch, f"concurrent-{count}")
            fresh = MissionInterface(store=self.store, cas=EvidenceCAS(self.store.paths), expected_mission_id="mission.1", canonical_snapshot=self.canonical)
            result = self._query(epoch, "inventory", page_size=1, cursor=result["next_cursor"], service=fresh)
            observed.extend(result["items"])
            self.assertLess(count, 10)
        self.assertEqual(observed, expected)
        self.assertEqual(result["completeness"], "exhausted")

    def test_native_cursor_seals_owner_population_and_old_cut_across_later_writes(self):
        epoch = self._start()
        expected = self._query(epoch, "inventory")["items"]
        result = self._query(epoch, "inventory", page_size=1)
        observed = list(result["items"])

        for candidate_id in ("candidate:aaa-after-cut", "candidate:zzz-after-cut"):
            recorded = self._execute(
                RECORD_CANDIDATE,
                {
                    "candidate_id": candidate_id,
                    "proposal_kind": "lemma",
                    "exact_statement": f"Later owner {candidate_id} is outside the cursor cut.",
                    "standing": {
                        "status": "open",
                        "basis": "This fixture is created after the first retrieval page.",
                    },
                },
                executive_epoch_id=epoch,
            )
            self.assertEqual(recorded["status"], "completed", recorded)
        advanced = self._execute(
            RECORD_STRATEGY,
            {
                "mission_continuation": "continue",
                "integrated_comparison": "Advance Strategy after the retrieval cut.",
                "reconsideration_conditions": [
                    {"condition": "Reconsider only on exact later evidence."}
                ],
            },
            executive_epoch_id=epoch,
        )
        self.assertEqual(advanced["status"], "completed", advanced)

        while result["next_cursor"] is not None:
            result = self._query(
                epoch,
                "inventory",
                page_size=1,
                cursor=result["next_cursor"],
            )
            observed.extend(result["items"])
        self.assertEqual(observed, expected)
        self.assertNotIn(
            "candidate:candidate.aaa-after-cut",
            {item["id"] for item in observed},
        )
        self.assertNotIn(
            "candidate:candidate.zzz-after-cut",
            {item["id"] for item in observed},
        )

    def test_sparse_indexed_search_avoids_owner_scan_and_preserves_fixed_cut(self):
        epoch = self._start()
        lease = self.interface._writer_lease()
        authority_digest = str(
            self.store.read_metadata()["canonical_authority_digest"]
        )
        self.assertEqual(retrieval_module._NATIVE_EXAMINATION_QUANTUM, 256)
        # Sparse results must seek indexed candidates, not consume one empty
        # page per former owner-examination quantum.
        for number in range(retrieval_module._NATIVE_EXAMINATION_QUANTUM):
            branch_id = f"branch.sparse.{number:04d}"
            outcome = self.store.commit_branch_revision(
                executive_epoch_id=epoch,
                mission_id="mission.1",
                branch_id=branch_id,
                payload={
                    "branch_id": branch_id,
                    "mission_id": "mission.1",
                    "question": (
                        "sparse-page-needle"
                        if number
                        >= retrieval_module._NATIVE_EXAMINATION_QUANTUM - 2
                        else f"nonmatching sparse owner {number:04d}"
                    ),
                },
                expected_head_revision=None,
                expected_head_payload_digest=None,
                lease=lease,
                command_id=f"sparse-search.branch.{number:04d}",
                actor="coordinating-codex",
                expected_canonical_authority_digest=authority_digest,
            )
            self.assertEqual(len(outcome.changed_heads), 1)
        expected_ids = [f"branch:branch.sparse.{number:04d}@1" for number in
                        range(retrieval_module._NATIVE_EXAMINATION_QUANTUM - 2,
                              retrieval_module._NATIVE_EXAMINATION_QUANTUM)]

        observed = []
        with mock.patch.object(
            self.store,
            "read_root_owner_page",
            wraps=self.store.read_root_owner_page,
        ) as owner_reads:
            page = self._query(
                epoch,
                "search",
                query="sparse-page-needle",
                kinds=["branch"],
                page_size=1,
            )
        self.assertEqual(owner_reads.call_count, 0)
        self.assertEqual([item["id"] for item in page["items"]], expected_ids[:1])
        self.assertEqual(page["state"], "present")
        self.assertEqual(page["completeness"], "page")
        self.assertIsNotNone(page["next_cursor"])

        post_cut_id = "branch.sparse.post-cut"
        self.store.commit_branch_revision(
            executive_epoch_id=epoch,
            mission_id="mission.1",
            branch_id=post_cut_id,
            payload={
                "branch_id": post_cut_id,
                "mission_id": "mission.1",
                "question": "sparse-page-needle after the fixed cut",
            },
            expected_head_revision=None,
            expected_head_payload_digest=None,
            lease=lease,
            command_id="sparse-search.branch.post-cut",
            actor="coordinating-codex",
            expected_canonical_authority_digest=authority_digest,
        )

        while True:
            observed.extend(item["id"] for item in page["items"])
            if page["next_cursor"] is None:
                break
            fresh = MissionInterface(
                store=self.store,
                cas=EvidenceCAS(self.store.paths),
                expected_mission_id="mission.1",
                canonical_snapshot=self.canonical,
            )
            with mock.patch.object(
                self.store,
                "read_root_owner_page",
                wraps=self.store.read_root_owner_page,
            ) as owner_reads:
                page = self._query(
                    epoch,
                    "search",
                    query="sparse-page-needle",
                    kinds=["branch"],
                    page_size=1,
                    cursor=page["next_cursor"],
                    service=fresh,
                )
            self.assertEqual(owner_reads.call_count, 0)
        self.assertEqual(observed, expected_ids)
        self.assertNotIn(f"branch:{post_cut_id}@1", observed)

        refreshed = []
        cursor = None
        while True:
            fresh = MissionInterface(
                store=self.store,
                cas=EvidenceCAS(self.store.paths),
                expected_mission_id="mission.1",
                canonical_snapshot=self.canonical,
            )
            selection = {
                "query": "sparse-page-needle",
                "kinds": ["branch"],
                "page_size": 1,
            }
            if cursor is not None:
                selection["cursor"] = cursor
            page = self._query(
                epoch,
                "search",
                service=fresh,
                **selection,
            )
            refreshed.extend(item["id"] for item in page["items"])
            cursor = page["next_cursor"]
            if cursor is None:
                break
        self.assertEqual(refreshed, expected_ids + [f"branch:{post_cut_id}@1"])

    def test_shared_native_examination_quantum_counts_filtered_candidates(self):
        first_epoch = self._start()
        strategy = self._execute(
            RECORD_STRATEGY,
            {
                "mission_continuation": "continue",
                "integrated_comparison": "Establish the sparse-mode checkpoint cut.",
                "reconsideration_conditions": [
                    {"condition": "Reconsider only on exact later evidence."}
                ],
            },
            executive_epoch_id=first_epoch,
        )
        self.assertEqual(strategy["status"], "completed", strategy)
        checkpoint = self._execute(CHECKPOINT, {}, executive_epoch_id=first_epoch)
        self.assertEqual(checkpoint["status"], "completed", checkpoint)
        checkpoint_id = str(checkpoint["result"]["checkpoint_id"])
        epoch = self._successor("shared-native-examination-quantum")
        for number in range(3):
            recorded = self._execute(
                RECORD_BRANCH,
                {
                    "branch_id": f"branch:branch.filtered-quantum.{number}",
                    "question": f"Filtered examination fixture {number}.",
                    "leverage_fingerprint": f"filtered-quantum-{number}",
                    "target_hook": "Remain outside the selected relation.",
                },
                executive_epoch_id=epoch,
            )
            self.assertEqual(recorded["status"], "completed", recorded)
            self._capture(epoch, f"filtered-quantum-{number}")
        candidate = self._execute(
            RECORD_CANDIDATE,
            {
                "candidate_id": "candidate:candidate.filtered-quantum",
                "proposal_kind": "lemma",
                "exact_statement": "Three exact hooks exercise a filtered page quantum.",
                "standing": {
                    "status": "open",
                    "basis": "Bound the shared native collector.",
                },
                "obligations": [
                    f"filtered-hook-{number}" for number in range(3)
                ],
            },
            executive_epoch_id=epoch,
        )
        self.assertEqual(candidate["status"], "completed", candidate)

        cases = (
            (
                "changes_since_checkpoint",
                {
                    "checkpoint_id": checkpoint_id,
                    "kinds": ["branch"],
                    "relations": ["advanced"],
                },
            ),
            (
                "hooks",
                {
                    "query": "never-matching-hook-query",
                    "owner_kinds": ["candidate"],
                    "hook_classes": ["unresolved"],
                },
            ),
            (
                "captures",
                {"pending_state": "fully_covered"},
            ),
        )
        with mock.patch.object(
            retrieval_module, "_NATIVE_EXAMINATION_QUANTUM", 2
        ):
            for mode, selection in cases:
                with self.subTest(mode=mode):
                    page = self._query(
                        epoch,
                        mode,
                        page_size=1,
                        **selection,
                    )
                    self.assertEqual(page["items"], [])
                    self.assertEqual(page["state"], "none")
                    self.assertEqual(page["completeness"], "page")
                    self.assertIsNotNone(page["next_cursor"])
                    pages = 1
                    while page["next_cursor"] is not None:
                        fresh = MissionInterface(
                            store=self.store,
                            cas=EvidenceCAS(self.store.paths),
                            expected_mission_id="mission.1",
                            canonical_snapshot=self.canonical,
                        )
                        page = self._query(
                            epoch,
                            mode,
                            page_size=1,
                            cursor=page["next_cursor"],
                            service=fresh,
                            **selection,
                        )
                        self.assertEqual(page["items"], [])
                        pages += 1
                    self.assertLess(pages, 20)
                    self.assertEqual(page["completeness"], "exhausted")
                    self.assertEqual(page["state"], "none")
                    self.assertIsNone(page["next_cursor"])

    def test_capture_pagination_pins_first_page_even_when_more_matching_captures_arrive(self):
        epoch = self._start()
        expected = {self._capture(epoch, f"initial-{n}") for n in range(3)}
        first = self._query(epoch, "captures", page_size=1)
        result = first
        actual = []
        while True:
            actual.extend(item["id"] for item in result["items"])
            if result["next_cursor"] is None:
                break
            self._capture(epoch, f"arrived-{len(actual)}")
            result = self._query(epoch, "captures", page_size=1, cursor=result["next_cursor"])
        self.assertEqual(set(actual), expected)
        self.assertEqual(len(actual), 3)
        self.assertTrue(all(item["pending_state"] == "current_pending" for item in first["items"]))
        self.assertGreater(len(self._query(epoch, "captures")["items"]), 3)

    def test_changes_cursor_pins_new_owner_revisions_across_matching_later_writes(self):
        first_epoch = self._start()
        strategy = self._execute(
            RECORD_STRATEGY,
            {
                "mission_continuation": "continue",
                "integrated_comparison": (
                    "Establish the exact pre-change continuation boundary."
                ),
                "reconsideration_conditions": [
                    {"condition": "Reconsider only on exact later evidence."}
                ],
            },
            executive_epoch_id=first_epoch,
        )
        self.assertEqual(strategy["status"], "completed", strategy)
        checkpoint = self._execute(CHECKPOINT, {}, executive_epoch_id=first_epoch)
        self.assertEqual(checkpoint["status"], "completed", checkpoint)
        baseline = str(checkpoint["result"]["checkpoint_id"])
        epoch = self._successor("changes-fixed-cut")
        branch_ids = []
        for number in range(3):
            branch_id = f"branch:branch.changes-fixed-cut.{number}"
            recorded = self._execute(
                RECORD_BRANCH,
                {
                    "branch_id": branch_id,
                    "question": f"Which fixed-cut change is number {number}?",
                    "leverage_fingerprint": f"changes-fixed-cut-{number}",
                    "target_hook": "Exercise exact changes seek continuation.",
                },
                executive_epoch_id=epoch,
            )
            self.assertEqual(recorded["status"], "completed", recorded)
            branch_ids.append(branch_id)

        selection = {
            "checkpoint_id": baseline,
            "kinds": ["branch"],
            "relations": ["new"],
        }
        expected = self._query(
            epoch,
            "changes_since_checkpoint",
            page_size=100,
            **selection,
        )["items"]
        page = self._query(
            epoch,
            "changes_since_checkpoint",
            page_size=1,
            **selection,
        )
        observed = list(page["items"])
        self.assertIsNotNone(page["next_cursor"])

        advanced = self._execute(
            RECORD_BRANCH,
            {
                "branch_id": branch_ids[1],
                "question": "Which fixed-cut change is number 1?",
                "leverage_fingerprint": "changes-fixed-cut-1",
                "target_hook": "Exercise exact changes seek continuation.",
                "revival_conditions": [
                    {
                        "condition": "Remain outside the already issued cursor cut.",
                        "owner_refs": [],
                    }
                ],
            },
            executive_epoch_id=epoch,
        )
        self.assertEqual(advanced["status"], "completed", advanced)
        added = self._execute(
            RECORD_BRANCH,
            {
                "branch_id": "branch:branch.changes-fixed-cut.after",
                "question": "Which matching owner was added after the cursor cut?",
                "leverage_fingerprint": "changes-fixed-cut-after",
                "target_hook": "Remain outside the already issued cursor horizon.",
            },
            executive_epoch_id=epoch,
        )
        self.assertEqual(added["status"], "completed", added)

        continued_handles = []
        while page["next_cursor"] is not None:
            fresh = MissionInterface(
                store=self.store,
                cas=EvidenceCAS(self.store.paths),
                expected_mission_id="mission.1",
                canonical_snapshot=self.canonical,
            )
            page = self._query(
                epoch,
                "changes_since_checkpoint",
                page_size=1,
                cursor=page["next_cursor"],
                service=fresh,
                **selection,
            )
            observed.extend(page["items"])
            continued_handles.extend(
                item["current_handle"] for item in page["items"]
            )
        self.assertEqual(observed, expected)
        self.assertIn(
            "branch:branch.changes-fixed-cut.1@1",
            continued_handles,
        )
        self.assertNotIn(
            "branch:branch.changes-fixed-cut.1@2",
            continued_handles,
        )

        current = self._query(
            epoch,
            "changes_since_checkpoint",
            page_size=100,
            **selection,
        )["items"]
        self.assertEqual(len(current), len(expected) + 1)
        self.assertIn(
            "branch:branch.changes-fixed-cut.1@2",
            {item["current_handle"] for item in current},
        )
        self.assertIn(
            "branch:branch.changes-fixed-cut.after@1",
            {item["current_handle"] for item in current},
        )

    def test_scope_tamper_filter_page_size_key_and_authority_changes_reject_without_writes(self):
        epoch = self._start()
        hook_fixture = self._execute(
            RECORD_CANDIDATE,
            {
                "candidate_id": "candidate:candidate.cursor-tamper-hooks",
                "proposal_kind": "lemma",
                "exact_statement": "Two authored obligations force a hook continuation.",
                "standing": {
                    "status": "open",
                    "basis": "Exercise closed, MAC-protected hook cursor fields.",
                },
                "obligations": ["cursor-hook-one", "cursor-hook-two"],
            },
            executive_epoch_id=epoch,
        )
        self.assertEqual(hook_fixture["status"], "completed", hook_fixture)
        first = self._query(epoch, "inventory", page_size=1)
        cursor = first["next_cursor"]
        self.assertIsNotNone(cursor)
        decoded = json.loads(base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4)))
        decoded["cut"] -= 1
        tampered = base64.urlsafe_b64encode(json.dumps(decoded).encode()).decode().rstrip("=")

        def tamper_native_position(source_cursor: str, *, field: str) -> str:
            outer = json.loads(
                base64.urlsafe_b64decode(
                    source_cursor + "=" * (-len(source_cursor) % 4)
                )
            )
            opaque = str(outer["token"])
            sealed = base64.urlsafe_b64decode(
                opaque + "=" * (-len(opaque) % 4)
            )
            payload, tag = sealed[:-32], sealed[-32:]
            document = json.loads(payload)
            position = json.loads(document["position"])
            if field == "last_key":
                position["seek"]["after"]["rowid"] += 1
            elif field == "horizon":
                position["horizon"]["candidate"] += 1
            elif field == "hook_root":
                position["seek"]["hook_after"]["descriptor"]["root_digest"] = (
                    "0" * 64
                )
            elif field == "hook_owner_reference":
                position["seek"]["hook_after"]["descriptor"][
                    "owner_reference"
                ]["payload_sha256"] = "0" * 64
            elif field == "hook_origin":
                position["seek"]["hook_after"]["descriptor"][
                    "origin_project_commit"
                ] += 1
            elif field == "hook_count":
                position["seek"]["hook_after"]["descriptor"][
                    "entry_count"
                ] += 1
            elif field == "hook_field_count":
                counts = position["seek"]["hook_after"]["descriptor"][
                    "field_counts"
                ]
                counts[next(iter(sorted(counts)))] += 1
            elif field == "strategy_root":
                position["seek"]["strategy"]["connection_index"][
                    "root_digest"
                ] = "0" * 64
            elif field == "strategy_origin":
                position["seek"]["strategy"]["connection_index"][
                    "origin_project_commit"
                ] += 1
            else:
                raise AssertionError(field)
            document["position"] = canonical_json_bytes(position).decode("utf-8")
            altered_opaque = base64.urlsafe_b64encode(
                canonical_json_bytes(document) + tag
            ).decode("ascii").rstrip("=")
            outer["token"] = altered_opaque
            return base64.urlsafe_b64encode(
                canonical_json_bytes(outer)
            ).decode("ascii").rstrip("=")

        tampered_last_key = tamper_native_position(cursor, field="last_key")
        tampered_horizon = tamper_native_position(cursor, field="horizon")
        before = deep_thaw(self.store.read_metadata())
        for altered, key in [
            ({"page_size": 2, "cursor": cursor}, self.key),
            ({"page_size": 1, "cursor": cursor, "kinds": ["branch"]}, self.key),
            ({"page_size": 1, "cursor": tampered}, self.key),
            ({"page_size": 1, "cursor": tampered_last_key}, self.key),
            ({"page_size": 1, "cursor": tampered_horizon}, self.key),
            ({"page_size": 1, "cursor": cursor}, {"cursor_mac_key": "ef" * 32}),
        ]:
            response = self.interface.execute_semantic_operation(self._request(RETRIEVE, {"mode": "inventory", "purpose": "Deliberate test selection.", **altered}), executive_epoch_id=epoch, root_query_context=key)
            self.assertEqual(response["status"], "rejected", response)
            self.assertEqual(response["error"]["code"], "mission_retrieval_cursor_invalid")
            self.assertNotIn(key["cursor_mac_key"], json.dumps(deep_thaw(response)))

        search = self._query(epoch, "search", query="theta", page_size=1)
        self.assertIsNotNone(search["next_cursor"])
        changed_query = self.interface.execute_semantic_operation(
            self._request(
                RETRIEVE,
                {
                    "mode": "search",
                    "purpose": "Deliberate test selection.",
                    "query": "changed-query",
                    "page_size": 1,
                    "cursor": search["next_cursor"],
                },
            ),
            executive_epoch_id=epoch,
            root_query_context=self.key,
        )
        self.assertEqual(changed_query["status"], "rejected", changed_query)
        self.assertEqual(
            changed_query["error"]["code"], "mission_retrieval_cursor_invalid"
        )

        hook = self._query(epoch, "hooks", page_size=1)
        self.assertIsNotNone(hook["next_cursor"])
        hook_cursor = str(hook["next_cursor"])
        for field in (
            "hook_root",
            "hook_owner_reference",
            "hook_origin",
            "hook_count",
            "hook_field_count",
            "strategy_root",
            "strategy_origin",
        ):
            altered_cursor = tamper_native_position(hook_cursor, field=field)
            rejected = self.interface.execute_semantic_operation(
                self._request(
                    RETRIEVE,
                    {
                        "mode": "hooks",
                        "purpose": "Deliberate test selection.",
                        "page_size": 1,
                        "cursor": altered_cursor,
                    },
                ),
                executive_epoch_id=epoch,
                root_query_context=self.key,
            )
            self.assertEqual(rejected["status"], "rejected", (field, rejected))
            self.assertEqual(
                rejected["error"]["code"], "mission_retrieval_cursor_invalid"
            )
        changed_relation = self.interface.execute_semantic_operation(
            self._request(
                RETRIEVE,
                {
                    "mode": "hooks",
                    "purpose": "Deliberate test selection.",
                    "page_size": 1,
                    "strategy_relation": "not_directly_referenced",
                    "cursor": hook_cursor,
                },
            ),
            executive_epoch_id=epoch,
            root_query_context=self.key,
        )
        self.assertEqual(changed_relation["status"], "rejected", changed_relation)
        self.assertEqual(
            changed_relation["error"]["code"], "mission_retrieval_cursor_invalid"
        )
        with mock.patch.object(self.interface, "_direct_epoch_authority", return_value=None):
            denied = self.interface.execute_semantic_operation(self._request(RETRIEVE, {"mode": "inventory", "purpose": "Deliberate test selection.", "page_size": 1, "cursor": cursor}), executive_epoch_id=epoch, root_query_context=self.key)
        self.assertEqual(denied["status"], "rejected", denied)
        self.assertEqual(before, deep_thaw(self.store.read_metadata()))

    def test_search_indexes_metadata_and_exact_child_lineage_not_raw_content(self):
        epoch = self._start()
        capture = self._capture(epoch, "needle-child")
        self.assertFalse(hasattr(self.interface, "_direct_frontier_panorama_items"))
        with mock.patch.object(self.interface, "_direct_frontier_panorama_items", side_effect=AssertionError("panorama"), create=True), mock.patch.object(self.store, "read_raw_capture_artifact_bytes", side_effect=AssertionError("body search")):
            match = self._query(epoch, "search", query="child:needle-child")
            self.assertEqual([row["id"] for row in match["items"]], [capture])
            self.assertTrue(match["items"][0]["artifacts"])
            self.assertEqual(self._query(epoch, "search", query="Quoted proof")["items"], [])
            page = self._query(epoch, "search", query="theta", page_size=1)
            self.assertEqual(len(page["items"]), 1)
            self.assertNotIn("readable_content", page["items"][0])

    def test_index_unavailable_is_not_a_partial_default_search_or_scan_fallback(self):
        epoch = self._start()
        before = deep_thaw(self.store.read_metadata())
        with (
            mock.patch.object(self.store, "read_owner_content_search_page",
                              side_effect=store_module.OwnerContentIndexUnavailableError("explicit index introduction required")) as indexed,
            mock.patch.object(self.store, "read_root_owner_page", side_effect=AssertionError("owner scan fallback")),
            mock.patch.object(self.store, "read_root_capture_page", side_effect=AssertionError("capture scan fallback")),
            mock.patch.object(self.store, "read_direct_recovery_facts", side_effect=AssertionError("historical fallback")),
        ):
            response = self.interface.execute_semantic_operation(
                self._request(RETRIEVE, {"mode": "search", "purpose": "Default complete route", "query": "theta"}),
                executive_epoch_id=epoch, root_query_context=self.key,
            )
        self.assertEqual(response["status"], "unavailable", response)
        self.assertEqual(response["error"]["code"], "mission_operation_unavailable")
        self.assertIsNone(response["result"])
        validate_semantic_result(RETRIEVE, response)
        self.assertEqual(set(indexed.call_args.kwargs["kinds"]), {
            "mission", "strategy", "branch", "candidate", "context", "evidence",
            "session", "capture", "capture-annotation",
        })
        self.assertEqual(set(indexed.call_args.kwargs["fields"]), {"id", "title", "content"})
        self.assertEqual(before, deep_thaw(self.store.read_metadata()))

    def test_mathematical_absent_query_does_not_prepare_irrelevant_capture_history(self):
        epoch = self._start()
        for number in range(32):
            self._capture(epoch, f"unrelated-search-custody-{number}")
        with (
            mock.patch.object(self.store, "read_root_owner_page", side_effect=AssertionError("owner population scan")),
            mock.patch.object(self.store, "read_root_capture_page", side_effect=AssertionError("irrelevant capture scan")),
            mock.patch.object(self.interface, "_read_recovery_owner_revision", wraps=self.interface._read_recovery_owner_revision) as owners,
        ):
            result = self._query(epoch, "search", query="no-such-mathematical-consumer-needle",
                                 kinds=["branch", "candidate", "evidence", "context", "strategy"])
        self.assertEqual(result["items"], [])
        self.assertEqual(result["completeness"], "exhausted")
        self.assertEqual(owners.call_count, 0)

    def test_indexed_cursor_binds_serving_descriptor_not_only_old_source_cut(self):
        epoch = self._start()
        selection = {"query": "theta", "kinds": ["branch", "strategy"], "page_size": 1}
        first = self._query(epoch, "search", **selection)
        self.assertIsNotNone(first["next_cursor"])
        original = self.store.read_owner_content_search_page

        def changed_descriptor(**kwargs):
            batch = deep_thaw(original(**kwargs))
            root = batch["owner_content_index"]["root"]
            digest = root["digest"]
            root["digest"] = ("0" if digest[0] != "0" else "1") + digest[1:]
            return batch

        with mock.patch.object(self.store, "read_owner_content_search_page", side_effect=changed_descriptor):
            rejected = self.interface.execute_semantic_operation(
                self._request(RETRIEVE, {"mode": "search", "purpose": "Deliberate test selection.",
                                         **selection, "cursor": first["next_cursor"]}),
                executive_epoch_id=epoch, root_query_context=self.key,
            )
        self.assertEqual(rejected["status"], "rejected", rejected)
        self.assertEqual(rejected["error"]["code"], "mission_retrieval_cursor_invalid")

    def test_exact_read_outcomes_preserve_order_and_local_unavailability(self):
        epoch, _, _, capture = self._prepare_exact_retrieval_fixture()
        evidence = self._source_evidence(epoch, "evidence.followup-unavailable", sources=(
            CaptureScope(capture.capture_id, 0, "The selected follow-up artifact."),
        ))
        artifact = f"capture-artifact:{capture.capture_id}#0"
        original = self.store.read_raw_capture_artifact_bytes
        def selected_unavailable(**kwargs):
            if kwargs["artifact_ordinal"] == 0:
                raise RawCaptureArtifactUnavailableError(capture_id=capture.capture_id, artifact_ordinal=0, disposition="quarantined")
            return original(**kwargs)
        with mock.patch.object(self.store, "read_raw_capture_artifact_bytes", side_effect=selected_unavailable):
            result = self._query(epoch, "read", ids=["mission:mission.1", artifact, "branch:absent", f"capture-artifact:{capture.capture_id}#1", f"evidence:{evidence.evidence_id}@1"])
        self.assertEqual([item["status"] for item in result["items"]], ["readable", "unavailable", "not_found", "readable", "readable"])
        self.assertEqual(result["items"][1]["requested_id"], artifact)
        self.assertNotIn("readable_content", result["items"][1])
        self.assertEqual(json.loads(result["items"][0]["readable_content"])["purpose"], self.genesis_seed["mission"]["purpose"])
        self.assertEqual(json.loads(result["items"][4]["readable_content"])["source_navigation"]["items"][0]["handle"], artifact)

    def _source_evidence(self, epoch, identity, *, sources=(), inputs=(), previous=None):
        record = prepare_evidence_meaning(
            authority=self.interface._direct_epoch_authority(epoch),
            evidence_id=identity,
            statement=f"Exact source-navigation fixture {identity}.",
            exact_scope="Synthetic fixture only; qualifications remain literal.",
            strength="conditional",
            semantic_role="result",
            authority_basis="Executive interpretation of the exact fixture.",
            sources=sources,
            interpreted_inputs=inputs,
            expected_dependency_heads={
                f"{item.kind}:{item.identity}": (item.revision, item.payload_sha256)
                for item in inputs
            },
            non_inferences=("No RH conclusion follows.",),
            expected_head_revision=None if previous is None else previous.revision,
            expected_head_payload_digest=None if previous is None else previous.payload_digest,
        )
        commit_evidence_meaning(
            self.store, record=record, lease=self.interface._writer_lease(),
            actor="source-navigation-fixture",
        )
        return record

    def test_exact_evidence_sources_preserve_revision_order_scopes_and_artifact_bytes(self):
        epoch, exact_text, exact_binary, capture = self._prepare_exact_retrieval_fixture()
        scopes = (
            CaptureScope(capture.capture_id, 1, "  The binary finite case only.\n"),
            CaptureScope(capture.capture_id, 0, {"lines": [1, 1], "qualification": "δ😀 only"}),
            CaptureScope(capture.capture_id, 0, {"description": "Same artifact, another scope.", "excluded": ["uniform claim"]}),
        )
        first = self._source_evidence(epoch, "evidence.source-navigation", sources=scopes)
        second = self._source_evidence(
            epoch, first.evidence_id, sources=(scopes[2],), previous=first,
        )
        before = deep_thaw(self.store.read_metadata())
        with mock.patch.object(
            self.store, "read_raw_capture_artifact_bytes",
            side_effect=AssertionError("Evidence navigation loaded a raw artifact body"),
        ), mock.patch.object(
            self.store, "read_evidence_meaning_revision",
            wraps=self.store.read_evidence_meaning_revision,
        ) as reads:
            result = self._query(epoch, "read", ids=[
                f"evidence:{first.evidence_id}@1", f"evidence:{first.evidence_id}",
            ])
        self.assertEqual(reads.call_args_list, [
            mock.call(first.evidence_id, revision=1),
            mock.call(first.evidence_id),
            mock.call(first.evidence_id, revision=2),
        ])
        self.assertEqual([item["id"] for item in result["items"]], [
            f"evidence:{first.evidence_id}@1", f"evidence:{first.evidence_id}@2",
        ])
        old, current = [json.loads(item["readable_content"]) for item in result["items"]]
        expected = [
            {"ordinal": index, "handle": f"capture-artifact:{scope.capture_id}#{scope.artifact_ordinal}",
             "exact_scope": deep_thaw(scope.exact_scope)}
            for index, scope in enumerate(scopes)
        ]
        self.assertEqual(old["source_navigation"], {
            "schema_version": "mathematical_research.evidence_source_navigation.v1",
            "relation": "recorded_capture_scopes", "items": expected,
        })
        self.assertEqual(current["source_navigation"]["items"], [{**expected[2], "ordinal": 0}])
        exact_owner = read_evidence_meaning(self.store, evidence_id=first.evidence_id, revision=1)
        self.assertEqual(exact_owner.sources, scopes)
        self.assertEqual(exact_owner.payload_digest, first.payload_digest)
        self.assertEqual(second.revision, 2)
        # A head chosen before an advance must not mix that revision's meaning
        # with a second unsuffixed source read. Return the genuine prior payload
        # for head resolution while the Store's current head is already r2.
        old_stored = self.store.read_evidence_meaning_revision(first.evidence_id, revision=1)
        original_read = self.store.read_evidence_meaning_revision
        def head_advanced(identity, revision=None):
            return old_stored if revision is None else original_read(identity, revision=revision)
        with mock.patch.object(self.store, "read_evidence_meaning_revision", side_effect=head_advanced) as raced_reads:
            raced = self._query(epoch, "read", ids=[f"evidence:{first.evidence_id}"])
        self.assertEqual(raced_reads.call_args_list, [
            mock.call(first.evidence_id), mock.call(first.evidence_id, revision=1),
        ])
        self.assertEqual(raced["items"][0]["id"], f"evidence:{first.evidence_id}@1")
        self.assertEqual(json.loads(raced["items"][0]["readable_content"]), old)
        for item in result["items"]:
            self.assertEqual(item["trust_class"], "semantic_owner_projection")
            self.assertEqual(item["completeness"], "complete_declared_semantic_projection")
        followed = self._query(epoch, "read", ids=list(dict.fromkeys(item["handle"] for item in expected)))
        by_id = {item["id"]: item for item in followed["items"]}
        for scope in scopes:
            item = by_id[f"capture-artifact:{scope.capture_id}#{scope.artifact_ordinal}"]
            self.assertEqual(item["status"], "readable")
            self.assertEqual(item["trust_class"], "untrusted_raw_material")
            transfer = json.loads(item["readable_content"])
            raw = (transfer["content"].encode(transfer["text_encoding"])
                   if transfer["representation"] == "text"
                   else base64.b64decode(transfer["content"]))
            self.assertEqual(raw, exact_text if scope.artifact_ordinal == 0 else exact_binary)
            self.assertEqual(raw, self.store.read_raw_capture_artifact_bytes(
                cas=self.cas, mission_id="mission.1",
                capture_id=capture.capture_id, artifact_ordinal=scope.artifact_ordinal,
            )[1])
        with mock.patch.object(
            self.store, "read_raw_capture_artifact_bytes",
            side_effect=AssertionError("Compact search loaded an artifact body"),
        ):
            searched = self._query(epoch, "search", query="source-navigation", kinds=["evidence"])
        self.assertTrue(searched["items"])
        self.assertNotIn("source_navigation", json.dumps(searched))
        self.assertNotIn("evidence_sources", self.interface._read_recovery_owner_revision(
            kind="evidence", identity=first.evidence_id, revision=1,
        ))
        self.assertEqual(before, deep_thaw(self.store.read_metadata()))

    def test_exact_evidence_synthesis_keeps_exact_inputs_and_expands_only_on_followup(self):
        epoch, _, _, capture = self._prepare_exact_retrieval_fixture()
        first = self._source_evidence(epoch, "evidence.source-premise-a", sources=(
            CaptureScope(capture.capture_id, 0, "First finite premise only."),
        ))
        other = self._source_evidence(epoch, "evidence.source-premise-b", sources=(
            CaptureScope(capture.capture_id, 1, "Second finite premise only."),
        ))
        derived = self._source_evidence(epoch, "evidence.source-synthesis", inputs=tuple(
            InterpretedOwnerReference("evidence", record.evidence_id, record.revision, record.payload_digest)
            for record in (first, other)
        ))
        self._source_evidence(epoch, first.evidence_id, sources=(
            CaptureScope(capture.capture_id, 1, "Later scope must not replace premise 1."),
        ), previous=first)
        before = deep_thaw(self.store.read_metadata())
        with mock.patch.object(
            self.store, "read_raw_capture_artifact_bytes",
            side_effect=AssertionError("Synthesis read expanded a raw source"),
        ), mock.patch.object(
            self.store, "read_evidence_meaning_revision",
            wraps=self.store.read_evidence_meaning_revision,
        ) as reads:
            result = self._query(epoch, "read", ids=[f"evidence:{derived.evidence_id}@1"])
        reads.assert_called_once_with(derived.evidence_id, revision=1)
        document = json.loads(result["items"][0]["readable_content"])
        self.assertEqual(document["subject"]["interpreted_owner_inputs"], [
            {"id": f"evidence:{record.evidence_id}", "revision": 1}
            for record in (first, other)
        ])
        self.assertEqual(document["source_navigation"]["items"], [])
        followed = self._query(epoch, "read", ids=[f"evidence:{first.evidence_id}@1"])
        self.assertEqual(json.loads(followed["items"][0]["readable_content"])["source_navigation"]["items"], [
            {"ordinal": 0, "handle": f"capture-artifact:{capture.capture_id}#0",
             "exact_scope": {"description": "First finite premise only."}},
        ])
        self.assertEqual(before, deep_thaw(self.store.read_metadata()))

    def test_exact_evidence_source_navigation_preserves_denial_and_integrity_boundaries(self):
        epoch, _, _, capture = self._prepare_exact_retrieval_fixture()
        record = self._source_evidence(epoch, "evidence.source-denial", sources=(
            CaptureScope(capture.capture_id, 0, "One scoped fixture source."),
        ))
        selected = f"evidence:{record.evidence_id}@1"
        with mock.patch.object(self.store, "read_evidence_meaning_revision", side_effect=StaleCommandError("disposed")):
            result = self._query(epoch, "read", ids=[selected, "mission:mission.1"])
        self.assertEqual([item["status"] for item in result["items"]], ["not_found", "readable"])
        self.assertNotIn("readable_content", result["items"][0])
        with mock.patch.object(self.store, "read_evidence_meaning_revision", side_effect=WorkspaceIntegrityError("shared integrity defect")):
            rejected = self.interface.execute_semantic_operation(
                self._request(RETRIEVE, {"mode": "read", "purpose": "Exact integrity boundary.", "ids": [selected]}),
                executive_epoch_id=epoch, root_query_context=self.key,
            )
        self.assertEqual(rejected["status"], "unavailable", rejected)
        self.assertEqual(rejected["error"]["code"], "mission_operation_unavailable")
        with mock.patch.object(self.interface, "_direct_epoch_authority", return_value=None), mock.patch.object(
            self.store, "read_evidence_meaning_revision", side_effect=AssertionError("read before authorization"),
        ):
            denied = self.interface.execute_semantic_operation(
                self._request(RETRIEVE, {"mode": "read", "purpose": "Unauthorized exact read.", "ids": [selected]}),
                executive_epoch_id=epoch, root_query_context=self.key,
            )
        self.assertNotEqual(denied["status"], "completed", denied)
        # A real authored-relation mutation is a shared integrity failure, not
        # a harmless missing source that could leave its Evidence readable.
        with closing(sqlite3.connect(self.store.paths.database)) as connection:
            connection.execute(
                "UPDATE evidence_capture_source SET exact_scope_json = ? "
                "WHERE evidence_id = ? AND evidence_revision = 1",
                ('{"description":"tampered qualification"}', record.evidence_id),
            )
            connection.commit()
        corrupted = self.interface.execute_semantic_operation(
            self._request(RETRIEVE, {"mode": "read", "purpose": "Read corrupted source relation.", "ids": [selected]}),
            executive_epoch_id=epoch, root_query_context=self.key,
        )
        self.assertEqual(corrupted["status"], "unavailable", corrupted)
        self.assertEqual(corrupted["error"]["code"], "mission_operation_unavailable")

    def test_native_inventory_avoids_recovery_materialization_and_reports_native_failure(self):
        epoch = self._start()
        before = deep_thaw(self.store.read_metadata())
        with mock.patch.object(self.store, "read_direct_recovery_facts", side_effect=WorkspaceIntegrityError("fixture integrity failure")):
            response = self.interface.execute_semantic_operation(self._request(RETRIEVE, {"mode": "inventory", "purpose": "Read actual owners."}), executive_epoch_id=epoch, root_query_context=self.key)
        self.assertEqual(response["status"], "completed", response)
        self.assertEqual(
            set(response["result"]["scope"]),
            {"basis", "checkpoint_id", "selection"},
        )
        with mock.patch.object(self.store, "read_root_owner_page", side_effect=WorkspaceIntegrityError("fixture native integrity failure")):
            response = self.interface.execute_semantic_operation(self._request(RETRIEVE, {"mode": "inventory", "purpose": "Read actual owners."}), executive_epoch_id=epoch, root_query_context=self.key)
        self.assertEqual(response["status"], "unavailable", response)
        self.assertEqual(response["error"]["code"], "mission_operation_unavailable")
        self.assertEqual(before, deep_thaw(self.store.read_metadata()))

    def test_all_native_collection_modes_reject_exhaustive_reconstruction_fallbacks(self):
        first = self._start()
        self._capture(first, "native-no-fallback")
        checkpoint = self._execute(CHECKPOINT, {}, executive_epoch_id=first)
        self.assertEqual(checkpoint["status"], "completed", checkpoint)
        epoch = self._successor("native-no-fallback-successor")

        forbidden = AssertionError("ordinary native retrieval reconstructed history")
        with (
            mock.patch.object(
                self.store,
                "read_direct_recovery_facts",
                side_effect=forbidden,
            ),
            mock.patch(
                "research_core.workspace_store._validated_journal_index",
                side_effect=forbidden,
            ),
            mock.patch(
                "research_core.workspace_store._direct_recovery_facts_from_connection",
                side_effect=forbidden,
            ),
        ):
            cases = (
                ("inventory", {}),
                ("search", {"query": "theta"}),
                (
                    "changes_since_checkpoint",
                    {"checkpoint_id": checkpoint["result"]["checkpoint_id"]},
                ),
                ("hooks", {}),
                ("captures", {}),
            )
            for mode, selection in cases:
                with self.subTest(mode=mode):
                    result = self._query(epoch, mode, **selection)
                    self.assertEqual(
                        set(result["scope"]),
                        {"basis", "checkpoint_id", "selection"},
                    )

    def test_checkpoint_sections_pin_exact_cut_and_changes_preserve_advanced_identity(self):
        epoch = self._start()
        unresolved = self._execute(
            RECORD_CANDIDATE,
            {
                "candidate_id": "candidate:candidate.explicit-unresolved",
                "proposal_kind": "lemma",
                "exact_statement": "One exact obstruction remains unresolved.",
                "standing": {
                    "status": "open",
                    "basis": "The stated objection remains to be discharged.",
                },
                "objections": ["The boundary term has no established sign."],
            },
            executive_epoch_id=epoch,
        )
        self.assertEqual(unresolved["status"], "completed", unresolved)
        unresolved_owner = self.store.read_candidate_revision(
            mission_id="mission.1",
            candidate_id="candidate.explicit-unresolved",
        )
        exact_unresolved_ref = {
            "kind": "candidate",
            "identity": "candidate.explicit-unresolved",
            "revision": unresolved_owner["revision"],
            "payload_sha256": unresolved_owner["payload_digest"],
        }
        plain_open = self._execute(
            RECORD_CANDIDATE,
            {
                "candidate_id": "candidate:candidate.plain-open",
                "proposal_kind": "lemma",
                "exact_statement": "Further work is pending on this open route.",
                "standing": {
                    "status": "open",
                    "basis": "No accepted resolution has been recorded.",
                },
            },
            executive_epoch_id=epoch,
        )
        self.assertEqual(plain_open["status"], "completed", plain_open)
        incomplete_work = self._execute(
            RECORD_CANDIDATE,
            {
                "candidate_id": "candidate:candidate.incomplete-work-only",
                "proposal_kind": "lemma",
                "exact_statement": "The checked finite cases do not settle the general claim.",
                "standing": {
                    "status": "open",
                    "basis": "The general argument has not been completed.",
                },
                "limitations": [
                    "Only the explicitly checked finite cases are currently supported."
                ],
                "non_inferences": [
                    "Do not infer the general claim from the checked finite cases."
                ],
            },
            executive_epoch_id=epoch,
        )
        self.assertEqual(incomplete_work["status"], "completed", incomplete_work)
        context = self._execute(
            RECORD_CONTEXT,
            {
                "context_id": "context:context.explicit-unresolved",
                "subject": "Exact unresolved-context fixture",
                "question": "Which named omission still needs attention?",
                "material": [
                    {
                        "id": "candidate:candidate.explicit-unresolved",
                        "why": "The current Candidate owns the exact unresolved objection.",
                    }
                ],
                "known_omissions": ["The endpoint estimate has not been established."],
                "restricted_uses": [],
                "restrictions": [],
                "dependencies": [],
            },
            executive_epoch_id=epoch,
        )
        self.assertEqual(context["status"], "completed", context)
        plain_branch = self._execute(
            RECORD_BRANCH,
            {
                "branch_id": "branch:branch.plain-open",
                "question": "Can this ordinary open route be completed?",
                "leverage_fingerprint": "plain-open-is-not-unresolved",
                "target_hook": "Continue ordinary work without an unresolved fact.",
            },
            executive_epoch_id=epoch,
        )
        self.assertEqual(plain_branch["status"], "completed", plain_branch)
        branch = self._execute(
            RECORD_BRANCH,
            {
                "branch_id": "branch:branch.revival-only",
                "question": "Could a later estimate revive this route?",
                "leverage_fingerprint": "revival-is-not-unresolved",
                "target_hook": "Retain the explicit revival condition only.",
                "revival_conditions": [
                    {
                        "condition": "Revive only if the missing estimate is proved.",
                        "owner_refs": [],
                    }
                ],
            },
            executive_epoch_id=epoch,
        )
        self.assertEqual(branch["status"], "completed", branch)
        strategy = self._execute(
            RECORD_STRATEGY,
            {
                "mission_continuation": "continue",
                "integrated_comparison": (
                    "Keep the explicit objection separate from lifecycle hooks."
                ),
                "reconsideration_conditions": [
                    {"condition": "Reconsider if a sharper bound is established."}
                ],
                "revival_conditions": [
                    {"condition": "Revive the discarded route only on exact evidence."}
                ],
            },
            executive_epoch_id=epoch,
        )
        self.assertEqual(strategy["status"], "completed", strategy)
        pending_capture_id = self._capture_pending_material(epoch)
        checkpoint_model_input = {}
        checkpoint = self._execute(
            CHECKPOINT,
            checkpoint_model_input,
            executive_epoch_id=epoch,
        )
        self.assertEqual(checkpoint["status"], "completed", checkpoint)
        baseline = checkpoint["result"]["checkpoint_id"]
        stored_checkpoint = self.store.read_continuation_checkpoint(
            checkpoint_id=baseline
        )
        self.assertIsNotNone(stored_checkpoint)
        assert stored_checkpoint is not None
        self.assertIn(
            {
                "owner_ref": exact_unresolved_ref,
                "json_pointer": "/objections/0",
            },
            deep_thaw(stored_checkpoint["document"]["unresolved_pointers"]),
        )
        reopened = MissionInterface.open(
            self.store.paths.root,
            "project.rh",
            "mission.1",
            canonical_snapshot=self.canonical,
        )
        self.interface = reopened
        self.store = reopened._store
        reconstruction = reopened.reconstruct()
        reconstructed_unresolved = {
            (item["retrieval_handle"], item["json_pointer"]): item
            for item in reconstruction["recovery_opening"]["hooks"]["unresolved"]
        }
        self.assertIn(
            (
                "candidate:candidate.explicit-unresolved@1",
                "/objections/0",
            ),
            reconstructed_unresolved,
        )
        self.assertEqual(
            reconstructed_unresolved[
                (
                    "candidate:candidate.explicit-unresolved@1",
                    "/objections/0",
                )
            ]["owner_ref"],
            exact_unresolved_ref,
        )
        self.assertIn(
            (
                "context:context.explicit-unresolved@1",
                "/known_omissions/0",
            ),
            reconstructed_unresolved,
        )
        epoch2 = self._authorize()
        self.goal_thread_id = "goal-thread:successor"
        self._bind(epoch2)
        for section in ("summary", "transitive_owner_refs", "causal_pointers", "unresolved_pointers", "pending_capture_locators"):
            result = self._query(epoch2, "checkpoint", checkpoint_id=baseline, section=section)
            self.assertEqual(result["scope"]["checkpoint_id"], baseline)
        with mock.patch.object(
            self.store,
            "read_direct_recovery_facts",
            side_effect=AssertionError(
                "compact unresolved checkpoint retrieval reconstructed history"
            ),
        ):
            unresolved = self._query(
                epoch2,
                "checkpoint",
                checkpoint_id=baseline,
                section="unresolved_pointers",
            )
        expected_unresolved = [
                {
                    "owner_handle": "candidate:candidate.explicit-unresolved@1",
                    "json_pointer": "/objections/0",
                },
                {
                    "owner_handle": "context:context.explicit-unresolved@1",
                    "json_pointer": "/known_omissions/0",
                },
            ]
        self.assertEqual(
            unresolved["items"],
            sorted(expected_unresolved, key=canonical_json_bytes),
        )
        self.assertNotIn(
            "candidate:candidate.plain-open@1",
            {item["owner_handle"] for item in unresolved["items"]},
        )
        unresolved_owner_handles = {
            item["owner_handle"] for item in unresolved["items"]
        }
        excluded_owner_handles = {
            "ordinary open Candidate": (
                f"{plain_open['result']['record_id']}@"
                f"{plain_open['result']['revision']}"
            ),
            "generic incomplete work": (
                f"{incomplete_work['result']['record_id']}@"
                f"{incomplete_work['result']['revision']}"
            ),
            "ordinary open Branch": (
                f"{plain_branch['result']['record_id']}@"
                f"{plain_branch['result']['revision']}"
            ),
            "revival condition": (
                f"{branch['result']['record_id']}@{branch['result']['revision']}"
            ),
            "Strategy reconsideration and revival conditions": (
                f"{strategy['result']['record_id']}@{strategy['result']['revision']}"
            ),
        }
        for category, owner_handle in excluded_owner_handles.items():
            with self.subTest(non_unresolved_category=category):
                self.assertNotIn(owner_handle, unresolved_owner_handles)
        self.assertNotIn(
            pending_capture_id,
            canonical_json_bytes(unresolved["items"]).decode("utf-8"),
        )
        result = self._query(epoch2, "checkpoint", section="transitive_owner_refs", page_size=1)
        self._capture(epoch2, "checkpoint-concurrent")
        if result["next_cursor"] is not None:
            next_page = self._query(epoch2, "checkpoint", section="transitive_owner_refs", page_size=1, cursor=result["next_cursor"])
            self.assertEqual(next_page["scope"]["checkpoint_id"], baseline)
        updated = self._execute(RECORD_STRATEGY, {"mission_continuation": "continue", "integrated_comparison": "Preserve the checkpoint while advancing exact Strategy meaning.", "reconsideration_conditions": [{"condition": "Reconsider upon exact new mathematical evidence."}]}, executive_epoch_id=epoch2)
        self.assertEqual(updated["status"], "completed", updated)
        changes = self._query(epoch2, "changes_since_checkpoint")
        strategy = next(item for item in changes["items"] if item["kind"] == "strategy")
        self.assertEqual(strategy["changes"], ["advanced"])
        self.assertNotEqual(strategy["before_handle"], strategy["current_handle"])

        later_checkpoint_result = self._execute(
            CHECKPOINT,
            {},
            executive_epoch_id=epoch2,
        )
        self.assertEqual(later_checkpoint_result["status"], "completed")
        later_checkpoint_id = later_checkpoint_result["result"]["checkpoint_id"]
        later_checkpoint = self.store.read_continuation_checkpoint(
            checkpoint_id=later_checkpoint_id
        )
        self.assertIsNotNone(later_checkpoint)
        assert later_checkpoint is not None
        epoch3 = self._successor("checkpoint-origin-authentication")

        with (
            mock.patch(
                "research_core.workspace_store._validated_journal_index",
                side_effect=AssertionError("exact checkpoint read built a journal index"),
            ),
            mock.patch(
                "research_core.workspace_store._validated_transition_journal_chain",
                side_effect=AssertionError("exact checkpoint read traversed the journal"),
            ),
            mock.patch(
                "research_core.workspace_store._direct_recovery_facts_from_connection",
                side_effect=AssertionError("exact checkpoint read materialized history"),
            ),
        ):
            exact_baseline = self.store.read_continuation_checkpoint(
                checkpoint_id=baseline
            )
        self.assertIsNotNone(exact_baseline)

        checkpoint_table = store_module.AuxiliaryTable.CONTINUATION_CHECKPOINT
        checkpoint_spec = store_module._AUXILIARY_SPECS[checkpoint_table]
        with sqlite3.connect(self.store.paths.database) as connection:
            connection.row_factory = sqlite3.Row
            original_row = connection.execute(
                "SELECT * FROM continuation_checkpoint WHERE checkpoint_id = ?",
                (baseline,),
            ).fetchone()
            assert original_row is not None
            original_values = {
                column: original_row[column]
                for column in checkpoint_spec.columns
            }
            forged_document = json.loads(str(original_row["document_json"]))
            forged_document["unresolved_pointers"][0]["json_pointer"] = (
                "/objections/999"
            )
            forged_json = canonical_json_bytes(forged_document).decode("utf-8")
            forged_values = {
                **original_values,
                "document_json": forged_json,
                "payload_digest": hashlib.sha256(
                    forged_json.encode("utf-8")
                ).hexdigest(),
            }
            forged_row_digest = store_module._row_digest(
                checkpoint_table.value,
                forged_values,
            )
            connection.execute(
                "UPDATE continuation_checkpoint SET document_json = ?, "
                "payload_digest = ?, row_digest = ? WHERE checkpoint_id = ?",
                (
                    forged_values["document_json"],
                    forged_values["payload_digest"],
                    forged_row_digest,
                    baseline,
                ),
            )

        reopened = MissionInterface.open(
            self.store.paths.root,
            "project.rh",
            "mission.1",
            canonical_snapshot=self.canonical,
        )
        self.interface = reopened
        self.store = reopened._store
        before_rejected_reads = deep_thaw(self.store.read_metadata())
        self.assertEqual(
            self.store.read_continuation_checkpoint(
                checkpoint_id=later_checkpoint_id
            )["checkpoint_id"],
            later_checkpoint_id,
        )
        for selector in (
            {"checkpoint_id": baseline},
            {"executive_epoch_id": stored_checkpoint["executive_epoch_id"]},
        ):
            with self.subTest(checkpoint_selector=selector):
                with self.assertRaises(WorkspaceIntegrityError):
                    self.store.read_continuation_checkpoint(**selector)
        rejected = self.interface.execute_semantic_operation(
            self._request(
                RETRIEVE,
                {
                    "mode": "checkpoint",
                    "purpose": "Reject a checkpoint with no exact origin.",
                    "checkpoint_id": baseline,
                    "section": "unresolved_pointers",
                },
            ),
            executive_epoch_id=epoch3,
            root_query_context=self.key,
        )
        self.assertEqual(rejected["status"], "unavailable", rejected)
        self.assertEqual(
            rejected["error"]["code"],
            "mission_operation_unavailable",
        )
        self.assertEqual(before_rejected_reads, deep_thaw(self.store.read_metadata()))

        rebound_values = {
            **original_values,
            "project_commit_no": later_checkpoint["project_commit_no"],
        }
        rebound_row_digest = store_module._row_digest(
            checkpoint_table.value,
            rebound_values,
        )
        with sqlite3.connect(self.store.paths.database) as connection:
            connection.execute(
                "UPDATE continuation_checkpoint SET project_commit_no = ?, "
                "document_json = ?, payload_digest = ?, row_digest = ? "
                "WHERE checkpoint_id = ?",
                (
                    rebound_values["project_commit_no"],
                    rebound_values["document_json"],
                    rebound_values["payload_digest"],
                    rebound_row_digest,
                    baseline,
                ),
            )
        with self.assertRaises(WorkspaceIntegrityError):
            self.store.read_continuation_checkpoint(checkpoint_id=baseline)
        self.assertEqual(before_rejected_reads, deep_thaw(self.store.read_metadata()))

        with sqlite3.connect(self.store.paths.database) as connection:
            connection.execute(
                "UPDATE project_commit SET created_by = ? WHERE commit_no = ?",
                (
                    "forged-checkpoint-origin",
                    later_checkpoint["project_commit_no"],
                ),
            )
        for selector in (
            {"checkpoint_id": later_checkpoint_id},
            {"executive_epoch_id": later_checkpoint["executive_epoch_id"]},
        ):
            with self.subTest(checkpoint_origin_selector=selector):
                with self.assertRaises(WorkspaceIntegrityError):
                    self.store.read_continuation_checkpoint(**selector)
        rejected_origin = self.interface.execute_semantic_operation(
            self._request(
                RETRIEVE,
                {
                    "mode": "checkpoint",
                    "purpose": "Reject a checkpoint with a corrupt commit origin.",
                    "checkpoint_id": later_checkpoint_id,
                    "section": "unresolved_pointers",
                },
            ),
            executive_epoch_id=epoch3,
            root_query_context=self.key,
        )
        self.assertEqual(rejected_origin["status"], "unavailable", rejected_origin)
        self.assertEqual(before_rejected_reads, deep_thaw(self.store.read_metadata()))

    def test_failed_interval_keeps_earlier_output_when_latest_failure_has_none(self):
        first = self._start()
        self._capture_pending_material(first)
        checkpoint = self._execute(CHECKPOINT, {}, executive_epoch_id=first)
        self.assertEqual(checkpoint["status"], "completed", checkpoint)
        earlier = self._successor("earlier-failed")
        output = self._capture(earlier, "earlier-output")
        self._fail(earlier)
        later = self._successor("later-failed")
        self._fail(later)
        active = self._successor("active-after-failures")
        orientation = self.interface.executive_orientation()
        usage_call = orientation["retrieval"]["usage_call"]
        self.assertEqual(
            usage_call,
            {"operation": "usage", "input": {"for_operation": "retrieve"}},
        )
        startup_json = json.dumps(orientation, ensure_ascii=False)
        self.assertNotIn(output, startup_json)
        self.assertNotIn(
            'Exact output for earlier-output. Quoted proof: {"closure":"δ😀"}',
            startup_json,
        )

        guide = deep_thaw(
            project_model_usage(for_operation=usage_call["input"]["for_operation"])
        )
        published = next(
            example["call"]
            for example in guide["examples"]
            if example["call"]["input"].get("view")
            == "failed_interval_or_late_output"
        )
        self.assertEqual(
            published,
            {
                "operation": "retrieve",
                "input": {
                    "mode": "captures",
                    "purpose": "Inspect retained output relevant to failed-epoch continuity.",
                    "view": "failed_interval_or_late_output",
                    "page_size": 25,
                },
            },
        )
        self.assertNotIn("ids", published["input"])
        response = self.interface.execute_semantic_operation(
            self._request(published["operation"], published["input"]),
            executive_epoch_id=active,
            root_query_context=self.key,
        )
        self.assertEqual(response["status"], "completed", response)
        result = deep_thaw(response["result"])
        self.assertEqual([item["id"] for item in result["items"]], [output])
        descriptor = result["items"][0]
        self.assertEqual(descriptor["origin_epoch_id"], earlier)
        self.assertEqual(descriptor["late_classification"], "at_or_before_terminal")
        artifact_handle = descriptor["artifacts"][0]["handle"]
        selected = self._query(active, "read", ids=[artifact_handle])["items"][0]
        self.assertEqual(selected["status"], "readable")
        self.assertEqual(selected["completeness"], "complete")
        self.assertEqual(selected["trust_class"], "untrusted_raw_material")
        self.assertEqual(
            json.loads(selected["readable_content"])["content"],
            'Exact output for earlier-output. Quoted proof: {"closure":"δ😀"}',
        )

    def test_late_old_failed_output_is_selected_but_assignments_and_covered_material_are_not(self):
        old = self._start()
        old_root = self.goal_thread_id
        old_output = self._capture(old, "old-pending")
        self._fail(old)
        checkpoint_epoch = self._successor("checkpoint-after-old-failure")
        self._capture_pending_material(checkpoint_epoch)
        checkpoint = self._execute(CHECKPOINT, {}, executive_epoch_id=checkpoint_epoch)
        self.assertEqual(checkpoint["status"], "completed", checkpoint)
        active = self._successor("active-after-cut")
        late = self._capture(old, "late-output", root=old_root)
        self._capture(old, "late-assignment", root=old_root, kind="assignment")
        self.assertEqual([item["id"] for item in self._query(active, "captures", view="failed_interval_or_late_output")["items"]], [late])
        narrow = self._query(active, "captures", thread_ids=["child:late-output"], origin_epoch_states=["failed_before_checkpoint"], late_classifications=["after_failed_terminal"], cut_relation="after_latest_checkpoint")
        self.assertEqual([item["id"] for item in narrow["items"]], [late])
        covered = self._execute(INTERPRET_MATERIAL, {"judgment": "reviewed_no_current_semantic_delta", "annotation_id": "capture-annotation:late-reviewed", "captured_material_id": late, "artifact_ordinal": 0, "exact_scope": "Complete artifact reviewed without new mathematical claim.", "coverage": "complete_artifact", "lifecycle": "active"}, executive_epoch_id=active)
        self.assertEqual(covered["status"], "completed", covered)
        self.assertEqual(self._query(active, "captures", view="failed_interval_or_late_output")["items"], [])
        self.assertIn(old_output, {item["id"] for item in self._query(active, "captures", pending_state="current_pending")["items"]})
        annotation = self._query(active, "read", ids=["capture-annotation:late-reviewed@1"])
        self.assertEqual(annotation["items"][0]["status"], "readable")

    def test_hook_values_resolve_exact_semantic_pointers_and_strategy_relation(self):
        epoch = self._start()
        hooks = self._query(epoch, "hooks")["items"]
        self.assertTrue(hooks)
        for item in hooks:
            selected = self._query(epoch, "read", ids=[item["owner_handle"]])["items"][0]
            value = json.loads(selected["readable_content"])
            for encoded in item["json_pointer"][1:].split("/"):
                token = encoded.replace("~1", "/").replace("~0", "~")
                value = value[int(token)] if isinstance(value, list) else value[token]
            self.assertEqual(value, item["value"])
        direct = self._query(epoch, "hooks", strategy_relation="directly_referenced")["items"]
        other = self._query(epoch, "hooks", strategy_relation="not_directly_referenced")["items"]
        self.assertTrue(all(item["strategy_connections"] for item in direct))
        self.assertTrue(all(not item["strategy_connections"] for item in other))
        self.assertEqual(len(hooks), len(direct) + len(other))

    def test_candidate_hook_index_excludes_other_genealogy_and_preserves_source_indices(self):
        synthetic_genealogy = [
            {
                "relation": "derived_from",
                "candidate_ref": {
                    "candidate_id": f"synthetic.nonrecombining.{index:04d}",
                    "revision": 1,
                    "digest_sha256": "1" * 64,
                },
            }
            for index in range(1_024)
        ]
        synthetic_genealogy.insert(
            777,
            {
                "relation": "recombines",
                "candidate_ref": {
                    "candidate_id": "synthetic.recombining",
                    "revision": 1,
                    "digest_sha256": "2" * 64,
                },
            },
        )
        entries = store_module._root_hook_entries(
            "candidate", {"genealogy": synthetic_genealogy}
        )
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0][0]["item_index"], 0)
        self.assertEqual(entries[0][1]["source_item_index"], 777)
        self.assertEqual(
            entries[0][1]["authored_value"]["relation"], "recombines"
        )

        epoch = self._start()
        source_ids = [
            f"candidate:candidate.genealogy-source-{suffix}"
            for suffix in ("a", "b", "c")
        ]
        for source_id in source_ids:
            created = self._execute(
                RECORD_CANDIDATE,
                {
                    "candidate_id": source_id,
                    "proposal_kind": "lemma",
                    "exact_statement": f"Source fixture {source_id}.",
                    "standing": {
                        "status": "open",
                        "basis": "Exact genealogy-index source fixture.",
                    },
                },
                executive_epoch_id=epoch,
            )
            self.assertEqual(created["status"], "completed", created)

        target_id = "candidate:candidate.genealogy-index-target"
        genealogy = [
            {"relation": relation, "candidate": {"id": source_id}}
            for relation, source_id in (
                ("derived_from", source_ids[0]),
                ("recombines", source_ids[1]),
                ("repair_of", source_ids[2]),
                ("alternative_to", source_ids[0]),
                ("recombines", source_ids[2]),
                ("split_from", source_ids[1]),
            )
        ]
        created = self._execute(
            RECORD_CANDIDATE,
            {
                "candidate_id": target_id,
                "proposal_kind": "lemma",
                "exact_statement": "Only explicit recombinations are root hooks.",
                "standing": {
                    "status": "open",
                    "basis": "Exercise filtered revision-local hook indexing.",
                },
                "genealogy": genealogy,
            },
            executive_epoch_id=epoch,
        )
        self.assertEqual(created["status"], "completed", created)
        exact = json.loads(
            self._query(epoch, "read", ids=[target_id])["items"][0][
                "readable_content"
            ]
        )
        expected = [
            (f"/genealogy/{index}", value)
            for index, value in enumerate(exact["genealogy"])
            if value["relation"] == "recombines"
        ]
        self.assertEqual(len(expected), 2)

        page = self._query(
            epoch,
            "hooks",
            query="candidate.genealogy-source-",
            owner_kinds=["candidate"],
            hook_classes=["recombination"],
            page_size=1,
        )
        observed = [
            (page["items"][0]["json_pointer"], page["items"][0]["value"])
        ]
        self.assertIsNotNone(page["next_cursor"])

        revised = self._execute(
            RECORD_CANDIDATE,
            {
                "candidate_id": target_id,
                "proposal_kind": "lemma",
                "exact_statement": "Only explicit recombinations are root hooks.",
                "standing": {
                    "status": "open",
                    "basis": "Exercise filtered revision-local hook indexing.",
                },
                "genealogy": [
                    {"relation": "derived_from", "candidate": {"id": source_ids[0]}}
                ],
            },
            executive_epoch_id=epoch,
        )
        self.assertEqual(revised["status"], "completed", revised)
        while page["next_cursor"] is not None:
            fresh = MissionInterface(
                store=self.store,
                cas=EvidenceCAS(self.store.paths),
                expected_mission_id="mission.1",
                canonical_snapshot=self.canonical,
            )
            page = self._query(
                epoch,
                "hooks",
                query="candidate.genealogy-source-",
                owner_kinds=["candidate"],
                hook_classes=["recombination"],
                page_size=1,
                cursor=page["next_cursor"],
                service=fresh,
            )
            observed.extend(
                (item["json_pointer"], item["value"])
                for item in page["items"]
            )
        self.assertEqual(observed, expected)
        self.assertEqual(
            self._query(
                epoch,
                "hooks",
                query="candidate.genealogy-source-",
                owner_kinds=["candidate"],
                hook_classes=["recombination"],
            )["items"],
            [],
        )

    def test_hook_cursor_authenticates_one_large_owner_once_and_resumes_its_index(self):
        epoch = self._start()
        hook_count = 128
        candidate_id = "candidate:candidate.hook-index-scale"
        values = [f"hook-index-scale-{index:04d}" for index in range(hook_count)]
        recorded = self._execute(
            RECORD_CANDIDATE,
            {
                "candidate_id": candidate_id,
                "proposal_kind": "lemma",
                "exact_statement": "A synthetic owner exercises indexed hook paging.",
                "standing": {
                    "status": "open",
                    "basis": "This is a retrieval-only scaling fixture.",
                },
                "obligations": values,
            },
            executive_epoch_id=epoch,
        )
        self.assertEqual(recorded["status"], "completed", recorded)
        strategy = self._execute(
            RECORD_STRATEGY,
            {
                "mission_continuation": "continue",
                "integrated_comparison": (
                    "Bind the exact first Candidate revision for fixed-cut paging."
                ),
                "reconsideration_conditions": [
                    {"condition": "Reconsider after the Candidate advances."}
                ],
                "owner_refs": [{"id": candidate_id}],
            },
            executive_epoch_id=epoch,
        )
        self.assertEqual(strategy["status"], "completed", strategy)
        cut = int(self.store.read_metadata()["current_project_commit"])

        original = store_module._root_hook_entries
        original_strategy = store_module._root_strategy_connection_entries
        candidate_projections = []
        strategy_projections = []
        observe_derivations = True

        def observed(kind, document):
            if observe_derivations and kind == "candidate" and document.get("candidate_id") == (
                "candidate.hook-index-scale"
            ):
                candidate_projections.append(len(canonical_json_bytes(document)))
            return original(kind, document)

        def observed_strategy(kind, document):
            if observe_derivations and kind == "strategy":
                strategy_projections.append(len(canonical_json_bytes(document)))
            return original_strategy(kind, document)

        forbidden = (
            "read_direct_recovery_facts",
            "_validated_journal_index",
            "_direct_recovery_facts_from_connection",
        )
        patches = [
            mock.patch.object(
                self.store,
                forbidden[0],
                side_effect=AssertionError("complete recovery called"),
            ),
            mock.patch.object(
                store_module,
                forbidden[1],
                side_effect=AssertionError("journal reconstruction called"),
            ),
            mock.patch.object(
                store_module,
                forbidden[2],
                side_effect=AssertionError("panorama reconstruction called"),
            ),
            mock.patch.object(
                store_module,
                "_root_hook_entries",
                side_effect=observed,
            ),
            mock.patch.object(
                store_module,
                "_root_strategy_connection_entries",
                side_effect=observed_strategy,
            ),
        ]
        for patcher in patches:
            patcher.start()
            self.addCleanup(patcher.stop)

        page = self._query(
            epoch,
            "hooks",
            query="hook-index-scale-",
            owner_kinds=["candidate"],
            hook_classes=["unresolved"],
            strategy_relation="directly_referenced",
            page_size=1,
        )
        first_cursor = page["next_cursor"]
        self.assertIsNotNone(first_cursor)
        outer = json.loads(
            base64.urlsafe_b64decode(
                first_cursor + "=" * (-len(first_cursor) % 4)
            )
        )
        self.assertEqual(outer["cut"], cut)

        observe_derivations = False
        new_values = [
            f"hook-index-scale-new-{index:04d}" for index in range(hook_count)
        ]
        later = self._execute(
            RECORD_CANDIDATE,
            {
                "candidate_id": candidate_id,
                "proposal_kind": "lemma",
                "exact_statement": "A synthetic owner exercises indexed hook paging.",
                "standing": {
                    "status": "open",
                    "basis": "This is a retrieval-only scaling fixture.",
                },
                "obligations": new_values,
            },
            executive_epoch_id=epoch,
        )
        self.assertEqual(later["status"], "completed", later)
        later_strategy = self._execute(
            RECORD_STRATEGY,
            {
                "mission_continuation": "continue",
                "integrated_comparison": (
                    "The successor Strategy deliberately drops the prior direct ref."
                ),
                "reconsideration_conditions": [
                    {"condition": "Reconsider on exact later evidence."}
                ],
            },
            executive_epoch_id=epoch,
        )
        self.assertEqual(later_strategy["status"], "completed", later_strategy)
        observe_derivations = True

        items = list(page["items"])
        pages = 1
        while page["next_cursor"] is not None:
            fresh = MissionInterface(
                store=self.store,
                cas=EvidenceCAS(self.store.paths),
                expected_mission_id="mission.1",
                canonical_snapshot=self.canonical,
            )
            page = self._query(
                epoch,
                "hooks",
                query="hook-index-scale-",
                owner_kinds=["candidate"],
                hook_classes=["unresolved"],
                strategy_relation="directly_referenced",
                page_size=1,
                cursor=page["next_cursor"],
                service=fresh,
            )
            pages += 1
            items.extend(page["items"])

        self.assertEqual(pages, hook_count)
        self.assertEqual(
            [item["json_pointer"] for item in items],
            [f"/obligations/{index}" for index in range(hook_count)],
        )
        self.assertEqual([item["value"] for item in items], values)
        self.assertTrue(all(item["strategy_connections"] for item in items))
        self.assertTrue(
            all(
                item["strategy_connections"] == items[0]["strategy_connections"]
                for item in items
            )
        )
        self.assertEqual(
            len({(item["owner_handle"], item["json_pointer"]) for item in items}),
            hook_count,
        )
        self.assertEqual(len(candidate_projections), 1, candidate_projections)
        self.assertEqual(len(strategy_projections), 1, strategy_projections)

        observe_derivations = False
        fresh_direct = self._query(
            epoch,
            "hooks",
            query="hook-index-scale-new-",
            owner_kinds=["candidate"],
            hook_classes=["unresolved"],
            strategy_relation="directly_referenced",
        )
        self.assertEqual(fresh_direct["items"], [])
        fresh_other = self._query(
            epoch,
            "hooks",
            query="hook-index-scale-new-",
            owner_kinds=["candidate"],
            hook_classes=["unresolved"],
            strategy_relation="not_directly_referenced",
            page_size=100,
        )
        fresh_other_items = list(fresh_other["items"])
        while fresh_other["next_cursor"] is not None:
            fresh_other = self._query(
                epoch,
                "hooks",
                query="hook-index-scale-new-",
                owner_kinds=["candidate"],
                hook_classes=["unresolved"],
                strategy_relation="not_directly_referenced",
                page_size=100,
                cursor=fresh_other["next_cursor"],
            )
            fresh_other_items.extend(fresh_other["items"])
        self.assertEqual(
            [item["owner_handle"] for item in fresh_other_items],
            ["candidate:candidate.hook-index-scale@2"] * hook_count,
        )
        self.assertEqual(
            [item["value"] for item in fresh_other_items], new_values
        )
        self.assertTrue(
            all(not item["strategy_connections"] for item in fresh_other_items)
        )

    @unittest.skipUnless(
        _ROOT6_PUBLIC_RETRIEVAL_SCALE_ENABLED,
        "explicit non-default native root6 public retrieval scale qualification",
    )
    def test_native_root6_public_retrieval_scale_qualification(self):
        """Exercise the real semantic facade without an exhaustive fallback.

        One staged native schema10/root6 workspace supplies the same Branch
        corpus to inventory, search, changes-since-checkpoint, and hooks.  A
        parallel Capture corpus exercises the route-specific coverage seek.
        The explicit opt-in gate keeps roughly sixty-five thousand
        semantic continuation requests out of ordinary local and CI runs.
        """

        epoch = self._start()
        strategy = self._execute(
            RECORD_STRATEGY,
            {
                "mission_continuation": "continue",
                "integrated_comparison": (
                    "Establish the exact pre-scale continuation boundary."
                ),
                "reconsideration_conditions": [
                    {"condition": "Reconsider only on exact later evidence."}
                ],
            },
            executive_epoch_id=epoch,
        )
        self.assertEqual(strategy["status"], "completed", strategy)
        checkpoint = self._execute(CHECKPOINT, {}, executive_epoch_id=epoch)
        self.assertEqual(checkpoint["status"], "completed", checkpoint)
        checkpoint_id = str(checkpoint["result"]["checkpoint_id"])
        epoch = self._successor("root-retrieval-scale")

        generated_branches: list[str] = []
        generated_captures: list[str] = []
        measurements: list[dict[str, object]] = []
        original_connect = sqlite3.connect
        original_owner_record = store_module._root_owner_record_at_cut_from_connection
        original_hook_entries = store_module._root_hook_entries
        original_strategy_entries = store_module._root_strategy_connection_entries
        cursor_key = {"cursor_mac_key": "dc" * 32}
        authority = self.interface._direct_epoch_authority(epoch)
        self.assertIsNotNone(authority)
        assert authority is not None
        lease = self.interface._writer_lease()
        authority_digest = str(
            self.store.read_metadata()["canonical_authority_digest"]
        )
        branch_template = None
        shared_blob = replace(
            EvidenceCAS(self.store.paths).ingest_bytes(
                b"Exact bounded public-retrieval scale observation.",
                original_name="root-retrieval-scale.txt",
                media_type="text/plain",
                encoding="utf-8",
            ).record,
            availability_state="verified_available",
        )

        def grow_branches(size: int) -> None:
            nonlocal branch_template
            while len(generated_branches) < size:
                ordinal = len(generated_branches)
                identity = f"branch.root-retrieval-scale.{ordinal:05d}"
                semantic_input = {
                    "branch_id": f"branch:{identity}",
                    "question": (
                        "Which exact bounded retrieval row is selected for "
                        f"qualification ordinal {ordinal:05d}?"
                    ),
                    "leverage_fingerprint": f"root-retrieval-scale-{ordinal:05d}",
                    "target_hook": "Retain one explicit, constant-size revival hook.",
                    "revival_conditions": [
                        {
                            "condition": (
                                "root-retrieval-scale-hook-" f"{ordinal:05d}"
                            ),
                            "owner_refs": [],
                        }
                    ],
                }
                if branch_template is None:
                    result = self._execute(
                        RECORD_BRANCH,
                        semantic_input,
                        executive_epoch_id=epoch,
                    )
                    self.assertEqual(result["status"], "completed", result)
                    branch_template = deep_thaw(
                        self.store.read_branch_revision(
                            mission_id="mission.1",
                            branch_id=identity,
                        )["payload"]
                    )
                else:
                    payload = deep_thaw(branch_template)
                    payload.update(
                        {
                            "branch_id": identity,
                            "question": semantic_input["question"],
                            "leverage_fingerprint": semantic_input[
                                "leverage_fingerprint"
                            ],
                            "revival_conditions": semantic_input[
                                "revival_conditions"
                            ],
                        }
                    )
                    outcome = self.store.commit_branch_revision(
                        executive_epoch_id=epoch,
                        mission_id="mission.1",
                        branch_id=identity,
                        payload=payload,
                        expected_head_revision=None,
                        expected_head_payload_digest=None,
                        lease=lease,
                        command_id=f"root6-public-scale.branch.{ordinal:05d}",
                        actor="root6-public-scale-fixture",
                        expected_canonical_authority_digest=authority_digest,
                    )
                    self.assertEqual(outcome.changed_heads[0].object_id.value, identity)
                generated_branches.append(identity)
                if len(generated_branches) % 1_000 == 0:
                    print(
                        "ROOT6_PUBLIC_SCALE_PROGRESS="
                        + json.dumps(
                            {
                                "phase": "branch_setup",
                                "records": len(generated_branches),
                            },
                            sort_keys=True,
                        ),
                        flush=True,
                    )

        def grow_captures(size: int) -> None:
            while len(generated_captures) < size:
                ordinal = len(generated_captures)
                observation_id = f"observation.root-retrieval-scale.{ordinal:05d}"
                capture_id = canonical_raw_capture_id(
                    project_id="project.rh",
                    capture_kind="output",
                    observation_id=observation_id,
                )
                self.store.commit_raw_capture(
                    record={
                        "capture_id": capture_id,
                        "mission_id": "mission.1",
                        "executive_epoch_id": epoch,
                        "capture_kind": "output",
                        "observation_id": observation_id,
                        "assignment_id": (
                            f"assignment.root-retrieval-scale.{ordinal:05d}"
                        ),
                        "provenance": {
                            "kind": "synthetic_public_retrieval_scale_fixture"
                        },
                        "completion": {"lifecycle": "completed"},
                    },
                    artifacts=(
                        {
                            "ordinal": 0,
                            "role": "result",
                            "logical_name": "root-retrieval-scale.txt",
                            "blob_sha256": shared_blob.sha256,
                        },
                    ),
                    blobs=(shared_blob,),
                    lease=lease,
                    command_id=f"root6-public-scale.capture.{ordinal:05d}",
                    actor="root6-public-scale-fixture",
                )
                generated_captures.append(f"capture:{capture_id}")
                if len(generated_captures) % 1_000 == 0:
                    print(
                        "ROOT6_PUBLIC_SCALE_PROGRESS="
                        + json.dumps(
                            {
                                "phase": "capture_setup",
                                "records": len(generated_captures),
                            },
                            sort_keys=True,
                        ),
                        flush=True,
                    )

        def selection_for(mode: str, page_size: int) -> dict[str, object]:
            common: dict[str, object] = {
                "mode": mode,
                "purpose": "Measure exact native root6 seek pagination.",
                "page_size": page_size,
            }
            if mode == "inventory":
                return {**common, "kinds": ["branch"]}
            if mode == "search":
                return {
                    **common,
                    "query": "root-retrieval-scale-hook-",
                    "kinds": ["branch"],
                    "fields": ["content"],
                }
            if mode == "changes_since_checkpoint":
                return {
                    **common,
                    "checkpoint_id": checkpoint_id,
                    "kinds": ["branch"],
                    "relations": ["new"],
                }
            if mode == "hooks":
                return {
                    **common,
                    "query": "root-retrieval-scale-hook-",
                    "hook_classes": ["revival"],
                    "owner_kinds": ["branch"],
                    "strategy_relation": "not_directly_referenced",
                }
            if mode == "captures":
                return common
            raise AssertionError(mode)

        def expected_count(mode: str, size: int) -> int:
            return size + 1 if mode == "inventory" else size

        def assert_item(mode: str, item: dict[str, object], ordinal: int) -> None:
            if mode == "inventory":
                if ordinal == 0:
                    expected_handle = "branch:branch.theta@1"
                else:
                    expected_handle = (
                        f"branch:{generated_branches[ordinal - 1]}@1"
                    )
                self.assertEqual(item["id"], expected_handle)
                self.assertEqual(item["kind"], "branch")
                return
            if mode == "captures":
                self.assertEqual(item["id"], generated_captures[ordinal])
                self.assertEqual(item["kind"], "capture")
                return
            identity = generated_branches[ordinal]
            handle = f"branch:{identity}@1"
            if mode == "search":
                self.assertEqual(item["id"], handle)
                self.assertEqual(
                    item["match_provenance"],
                    [
                        {
                            "field": "content",
                            "query": "root-retrieval-scale-hook-",
                        }
                    ],
                )
                return
            if mode == "changes_since_checkpoint":
                self.assertEqual(item["kind"], "branch")
                self.assertEqual(item["before_handle"], None)
                self.assertEqual(item["current_handle"], handle)
                self.assertEqual(item["changes"], ["new"])
                return
            self.assertEqual(mode, "hooks")
            self.assertEqual(item["owner_handle"], handle)
            self.assertEqual(item["hook_class"], "revival")
            self.assertEqual(item["semantic_field"], "revival_conditions")
            self.assertEqual(item["json_pointer"], "/revival_conditions/0")
            self.assertEqual(
                item["value"],
                {
                    "condition": f"root-retrieval-scale-hook-{ordinal:05d}",
                    "owner_refs": [],
                },
            )
            self.assertEqual(item["strategy_connections"], [])

        def exhaust(mode: str, *, size: int, page_size: int) -> dict[str, object]:
            expected = expected_count(mode, size)
            expected_pages = (expected + page_size - 1) // page_size
            meter = {
                "connections": 0,
                "statements": 0,
                "vm_steps": 0,
                "owner_reads": 0,
                "hook_derivations": 0,
                "strategy_derivations": 0,
                "violations": [],
            }
            totals = {
                "pages": 0,
                "items": 0,
                "statements": 0,
                "vm_steps": 0,
                "owner_reads": 0,
                "hook_derivations": 0,
                "strategy_derivations": 0,
                "min_page_statements": None,
                "max_page_statements": 0,
                "min_page_vm_steps": None,
                "max_page_vm_steps": 0,
                "first_quarter_max_statements": 0,
                "last_quarter_max_statements": 0,
                "first_quarter_max_vm_steps": 0,
                "last_quarter_max_vm_steps": 0,
            }
            exact_database_uri = self.store.paths.database.resolve().as_uri()
            expected_database_target = exact_database_uri + "?mode=ro"

            def instrumented_connect(*args, **kwargs):
                connection = original_connect(*args, **kwargs)
                target = str(args[0]) if args else str(kwargs.get("database", ""))
                meter["connections"] += 1
                if not (
                    kwargs.get("uri") is True
                    and target == expected_database_target
                ):
                    meter["violations"].append(
                        "unexpected SQLite target or access mode"
                    )

                # Meter every connection, including an accidental ordinary or
                # alternate-target connection, so no same-request SQL/VM work
                # can escape the structural qualification.
                def trace(statement: str) -> None:
                    meter["statements"] += 1
                    if re.match(
                        r"\s*(?:INSERT|UPDATE|DELETE|REPLACE|CREATE|DROP|ALTER|"
                        r"VACUUM|REINDEX|ATTACH|DETACH)\b",
                        statement,
                        re.I,
                    ):
                        meter["violations"].append("write SQL during retrieval")
                    if re.search(
                        r"PRAGMA\s+(?:\w+\.)?(?:integrity_check|foreign_key_check)\b",
                        statement,
                        re.I,
                    ):
                        meter["violations"].append("complete integrity PRAGMA")

                def progress() -> int:
                    meter["vm_steps"] += 100
                    return 0

                connection.set_trace_callback(trace)
                connection.set_progress_handler(progress, 100)
                return connection

            def observed_owner(*args, **kwargs):
                meter["owner_reads"] += 1
                return original_owner_record(*args, **kwargs)

            def observed_hooks(*args, **kwargs):
                meter["hook_derivations"] += 1
                return original_hook_entries(*args, **kwargs)

            def observed_strategy(*args, **kwargs):
                meter["strategy_derivations"] += 1
                return original_strategy_entries(*args, **kwargs)

            before_metadata = deep_thaw(self.store.read_metadata())
            cursor = None
            first_cut = int(before_metadata["current_project_commit"])
            selection = selection_for(mode, page_size)
            started = time.perf_counter()
            forbidden = AssertionError(
                f"native {mode} retrieval selected an exhaustive helper"
            )
            patches = (
                mock.patch.object(sqlite3, "connect", new=instrumented_connect),
                mock.patch.object(
                    self.store,
                    "read_direct_recovery_facts",
                    side_effect=forbidden,
                ),
                mock.patch.object(
                    self.store,
                    "read_raw_capture_artifact_bytes",
                    side_effect=forbidden,
                ),
                mock.patch.object(
                    self.store,
                    "read_metadata",
                    side_effect=forbidden,
                ),
                mock.patch.object(
                    self.interface,
                    "reconstruct",
                    side_effect=forbidden,
                ),
                mock.patch.object(
                    type(self.store),
                    "_verify_current_boundary",
                    side_effect=forbidden,
                ),
                mock.patch.object(
                    type(self.store),
                    "verify_integrity",
                    side_effect=forbidden,
                ),
                mock.patch.object(
                    store_module,
                    "_validated_journal_index",
                    side_effect=forbidden,
                ),
                mock.patch.object(
                    store_module,
                    "_validated_transition_journal_chain",
                    side_effect=forbidden,
                ),
                mock.patch.object(
                    store_module,
                    "_direct_recovery_facts_from_connection",
                    side_effect=forbidden,
                ),
                mock.patch.object(
                    store_module,
                    "_capture_coverage_current_entries",
                    side_effect=forbidden,
                ),
                mock.patch.object(
                    store_module,
                    "_capture_coverage_evidence_scopes_from_connection",
                    side_effect=forbidden,
                ),
                mock.patch.object(
                    store_module,
                    "_root_owner_record_at_cut_from_connection",
                    new=observed_owner,
                ),
                mock.patch.object(
                    store_module,
                    "_root_hook_entries",
                    new=observed_hooks,
                ),
                mock.patch.object(
                    store_module,
                    "_root_strategy_connection_entries",
                    new=observed_strategy,
                ),
                mock.patch.object(
                    retrieval_module,
                    "_discovery",
                    side_effect=forbidden,
                ),
                mock.patch.object(
                    retrieval_module,
                    "_hooks",
                    side_effect=forbidden,
                ),
                mock.patch.object(
                    retrieval_module,
                    "_filtered_captures",
                    side_effect=forbidden,
                ),
                mock.patch.object(
                    retrieval_module,
                    "change_descriptors_in_snapshot",
                    side_effect=forbidden,
                ),
            )
            with ExitStack() as stack:
                for patch in patches:
                    stack.enter_context(patch)
                while True:
                    page_before = {
                        key: int(meter[key])
                        for key in (
                            "connections",
                            "statements",
                            "vm_steps",
                            "owner_reads",
                            "hook_derivations",
                            "strategy_derivations",
                        )
                    }
                    request_selection = dict(selection)
                    if cursor is not None:
                        request_selection["cursor"] = cursor
                    response = self.interface.execute_semantic_operation(
                        self._request(RETRIEVE, request_selection),
                        executive_epoch_id=epoch,
                        root_query_context=cursor_key,
                    )
                    self.assertEqual(response["status"], "completed", response)
                    page = deep_thaw(response["result"])
                    page_no = int(totals["pages"]) + 1
                    items = list(page["items"])
                    remaining = expected - int(totals["items"])
                    self.assertEqual(len(items), min(page_size, remaining))
                    self.assertTrue(items)
                    for item in items:
                        assert_item(mode, item, int(totals["items"]))
                        totals["items"] = int(totals["items"]) + 1

                    page_cost = {
                        key: int(meter[key]) - page_before[key]
                        for key in page_before
                    }
                    self.assertEqual(page_cost["connections"], 1, page_cost)
                    if mode == "hooks":
                        self.assertLessEqual(
                            page_cost["hook_derivations"],
                            len(items) + 2,
                            page_cost,
                        )
                        self.assertLessEqual(
                            page_cost["strategy_derivations"],
                            1 if page_no == 1 else 0,
                            page_cost,
                        )
                    else:
                        self.assertEqual(page_cost["hook_derivations"], 0, page_cost)
                        self.assertEqual(
                            page_cost["strategy_derivations"], 0, page_cost
                        )
                    if mode == "search":
                        self.assertLessEqual(
                            page_cost["owner_reads"], len(items) + 2, page_cost
                        )

                    for key in (
                        "statements",
                        "vm_steps",
                        "owner_reads",
                        "hook_derivations",
                        "strategy_derivations",
                    ):
                        totals[key] = int(totals[key]) + page_cost[key]
                    for key in ("statements", "vm_steps"):
                        minimum = f"min_page_{key}"
                        maximum = f"max_page_{key}"
                        totals[minimum] = (
                            page_cost[key]
                            if totals[minimum] is None
                            else min(int(totals[minimum]), page_cost[key])
                        )
                        totals[maximum] = max(int(totals[maximum]), page_cost[key])
                    quarter = max(1, expected_pages // 4)
                    if page_no <= quarter:
                        totals["first_quarter_max_statements"] = max(
                            int(totals["first_quarter_max_statements"]),
                            page_cost["statements"],
                        )
                        totals["first_quarter_max_vm_steps"] = max(
                            int(totals["first_quarter_max_vm_steps"]),
                            page_cost["vm_steps"],
                        )
                    if page_no > expected_pages - quarter:
                        totals["last_quarter_max_statements"] = max(
                            int(totals["last_quarter_max_statements"]),
                            page_cost["statements"],
                        )
                        totals["last_quarter_max_vm_steps"] = max(
                            int(totals["last_quarter_max_vm_steps"]),
                            page_cost["vm_steps"],
                        )

                    self.assertEqual(page["mode"], mode)
                    self.assertEqual(page["scope"]["basis"], "current_at_first_page")
                    self.assertEqual(
                        set(page["scope"]),
                        {"basis", "checkpoint_id", "selection"},
                    )
                    self.assertEqual(page["scope"]["checkpoint_id"], checkpoint_id)
                    cursor = page["next_cursor"]
                    totals["pages"] = page_no
                    if page_no < expected_pages:
                        self.assertEqual(page["completeness"], "page")
                        self.assertIsNotNone(cursor)
                        outer = json.loads(
                            base64.urlsafe_b64decode(
                                str(cursor) + "=" * (-len(str(cursor)) % 4)
                            )
                        )
                        self.assertEqual(set(outer), {"cut", "checkpoint_id", "token"})
                        self.assertEqual(outer["cut"], first_cut)
                        self.assertEqual(outer["checkpoint_id"], checkpoint_id)
                    else:
                        self.assertEqual(page["completeness"], "exhausted")
                        self.assertIsNone(cursor)
                        break

            elapsed = time.perf_counter() - started
            self.assertEqual(meter["violations"], [], meter["violations"])
            self.assertEqual(int(totals["pages"]), expected_pages)
            self.assertEqual(int(totals["items"]), expected)
            self.assertEqual(
                deep_thaw(self.store.read_metadata()), before_metadata
            )
            self.assertLessEqual(
                int(totals["last_quarter_max_statements"]),
                int(totals["first_quarter_max_statements"]) * 1.35 + 32,
                totals,
            )
            self.assertLessEqual(
                int(totals["last_quarter_max_vm_steps"]),
                int(totals["first_quarter_max_vm_steps"]) * 1.50 + 5_000,
                totals,
            )
            result = {
                "mode": mode,
                "records": size,
                "page_size": page_size,
                **totals,
                "elapsed_seconds": round(elapsed, 6),
                "vm_step_quantum": 100,
                "cut": first_cut,
            }
            print(
                "ROOT6_PUBLIC_SCALE_CELL="
                + json.dumps(result, sort_keys=True),
                flush=True,
            )
            return result

        for size in (100, 300, 1_000, 10_000):
            if size != 300:
                grow_branches(size)
            grow_captures(size)
            modes = (
                ("inventory", "search", "changes_since_checkpoint", "hooks", "captures")
                if size != 300
                else ("captures",)
            )
            page_sizes = (25, 8, 1) if size != 300 else (1,)
            for mode in modes:
                for page_size in page_sizes:
                    measurements.append(
                        exhaust(mode, size=size, page_size=page_size)
                    )

        for mode in (
            "inventory",
            "search",
            "changes_since_checkpoint",
            "hooks",
            "captures",
        ):
            for page_size in (1, 8, 25):
                selected = [
                    item
                    for item in measurements
                    if item["mode"] == mode
                    and item["page_size"] == page_size
                    and item["records"] in {100, 1_000, 10_000}
                ]
                self.assertEqual(
                    [item["records"] for item in selected],
                    [100, 1_000, 10_000],
                    selected,
                )
                for smaller, larger in zip(selected, selected[1:]):
                    smaller_count = int(smaller["items"])
                    larger_count = int(larger["items"])
                    near_linear_ratio = (
                        larger_count * larger_count.bit_length()
                    ) / (smaller_count * smaller_count.bit_length())
                    self.assertLessEqual(
                        int(larger["statements"]),
                        int(smaller["statements"])
                        * near_linear_ratio
                        * 1.35
                        + 1_000,
                        selected,
                    )
                    self.assertLessEqual(
                        int(larger["vm_steps"]),
                        int(smaller["vm_steps"])
                        * near_linear_ratio
                        * 1.65
                        + 10_000,
                        selected,
                    )
                    logarithmic_ratio = (
                        larger_count.bit_length() / smaller_count.bit_length()
                    )
                    self.assertLessEqual(
                        int(larger["max_page_statements"]),
                        int(smaller["max_page_statements"])
                        * logarithmic_ratio
                        * 1.35
                        + 32,
                        selected,
                    )
                    # VM work includes both the authenticated projection path
                    # and SQLite's growing digest-index path.  A squared-log
                    # per-page envelope still rejects any linear scan of N.
                    self.assertLessEqual(
                        int(larger["max_page_vm_steps"]),
                        int(smaller["max_page_vm_steps"])
                        * logarithmic_ratio**2
                        * 1.50
                        + 5_000,
                        selected,
                    )

        for mode in (
            "inventory",
            "search",
            "changes_since_checkpoint",
            "hooks",
            "captures",
        ):
            for size in (100, 1_000, 10_000):
                selected = {
                    int(item["page_size"]): item
                    for item in measurements
                    if item["mode"] == mode and item["records"] == size
                }
                self.assertEqual(set(selected), {1, 8, 25})
                one = selected[1]
                for page_size in (8, 25):
                    larger_page = selected[page_size]
                    self.assertLessEqual(
                        int(larger_page["max_page_statements"]),
                        int(one["max_page_statements"]) * (page_size + 2) + 32,
                        selected,
                    )
                    self.assertLessEqual(
                        int(larger_page["max_page_vm_steps"]),
                        int(one["max_page_vm_steps"]) * (page_size + 2) + 5_000,
                        selected,
                    )

        print(
            "ROOT6_PUBLIC_RETRIEVAL_SCALE_QUALIFICATION="
            + json.dumps(measurements, sort_keys=True),
            flush=True,
        )

    def test_hook_projection_is_root6_bound_and_full_audit_rederives_its_owner(self):
        epoch = self._start()
        candidate_id = "candidate:candidate.hook-transition-binding"
        values = ["bound-hook-zero", "bound-hook-one", "bound-hook-two"]
        recorded = self._execute(
            RECORD_CANDIDATE,
            {
                "candidate_id": candidate_id,
                "proposal_kind": "lemma",
                "exact_statement": "The hook projection has one exact root6 origin.",
                "standing": {
                    "status": "open",
                    "basis": "Exercise transition-bound indexed continuation.",
                },
                "obligations": values,
            },
            executive_epoch_id=epoch,
        )
        self.assertEqual(recorded["status"], "completed", recorded)
        later = self._execute(
            RECORD_BRANCH,
            {
                "branch_id": "branch:branch.after-hook-binding",
                "question": "Can the exact prior projection remain independently bound?",
                "leverage_fingerprint": "hook-transition-binding",
                "target_hook": "Leave the Candidate origin behind the current cut.",
            },
            executive_epoch_id=epoch,
        )
        self.assertEqual(later["status"], "completed", later)

        first = self._query(
            epoch,
            "hooks",
            query="bound-hook-",
            owner_kinds=["candidate"],
            hook_classes=["unresolved"],
            page_size=1,
        )
        self.assertEqual(len(first["items"]), 1)
        self.assertIn(first["items"][0]["value"], values)
        first_value = first["items"][0]["value"]
        self.assertIsNotNone(first["next_cursor"])
        outer = json.loads(
            base64.urlsafe_b64decode(
                first["next_cursor"] + "=" * (-len(first["next_cursor"]) % 4)
            )
        )
        opaque = str(outer["token"])
        sealed = base64.urlsafe_b64decode(
            opaque + "=" * (-len(opaque) % 4)
        )
        cursor_document = json.loads(sealed[:-32])
        position = json.loads(cursor_document["position"])
        descriptor = position["seek"]["hook_after"]["descriptor"]
        strategy_descriptor = position["seek"]["strategy"]["connection_index"]
        with self.store.direct_recovery_read_scope():
            hook_mutations = {
                "root": lambda value: value.__setitem__(
                    "root_digest", "0" * 64
                ),
                "owner": lambda value: value["owner_reference"].__setitem__(
                    "payload_sha256", "0" * 64
                ),
                "origin": lambda value: value.__setitem__(
                    "origin_project_commit", value["origin_project_commit"] + 1
                ),
                "entry-count": lambda value: value.__setitem__(
                    "entry_count", value["entry_count"] + 1
                ),
                "field-count": lambda value: value["field_counts"].__setitem__(
                    "obligations", value["field_counts"]["obligations"] + 1
                ),
            }
            for name, mutate in hook_mutations.items():
                with self.subTest(direct_hook_descriptor=name):
                    altered = json.loads(json.dumps(descriptor))
                    mutate(altered)
                    with self.assertRaises(WorkspaceIntegrityError):
                        self.store.read_root_hook_item(
                            descriptor=altered,
                            hook_class="unresolved",
                            semantic_field="obligations",
                            item_index=1,
                        )

            for name, mutate in (
                (
                    "root",
                    lambda value: value.__setitem__(
                        "root_digest", "0" * 64
                    ),
                ),
                (
                    "owner",
                    lambda value: value["owner_reference"].__setitem__(
                        "payload_sha256", "0" * 64
                    ),
                ),
                (
                    "origin",
                    lambda value: value.__setitem__(
                        "origin_project_commit",
                        value["origin_project_commit"] + 1,
                    ),
                ),
                (
                    "entry-count",
                    lambda value: value.__setitem__(
                        "entry_count", value["entry_count"] + 1
                    ),
                ),
            ):
                with self.subTest(direct_strategy_descriptor=name):
                    altered = json.loads(json.dumps(strategy_descriptor))
                    mutate(altered)
                    with self.assertRaises(WorkspaceIntegrityError):
                        self.store.read_root_strategy_connections(
                            descriptor=altered,
                            owner_reference=descriptor["owner_reference"],
                        )

        with closing(sqlite3.connect(self.store.paths.database)) as connection:
            connection.execute(
                "UPDATE candidate_revision SET payload_json = '{}' "
                "WHERE object_id = ? AND revision = 1",
                (candidate_id.removeprefix("candidate:"),),
            )
            connection.commit()
        second = self._query(
            epoch,
            "hooks",
            query="bound-hook-",
            owner_kinds=["candidate"],
            hook_classes=["unresolved"],
            page_size=1,
            cursor=first["next_cursor"],
        )
        self.assertEqual(len(second["items"]), 1)
        self.assertIn(second["items"][0]["value"], values)
        self.assertNotEqual(second["items"][0]["value"], first_value)
        with self.assertRaises(WorkspaceIntegrityError):
            self.store.verify_integrity()

    def test_capture_continuation_preserves_as_of_pending_coverage(self):
        epoch = self._start()
        captures = sorted(self._capture(epoch, f"coverage-{number}") for number in range(2))
        first = self._query(epoch, "captures", page_size=1)
        covered = self._execute(INTERPRET_MATERIAL, {"judgment": "reviewed_no_current_semantic_delta", "annotation_id": "capture-annotation:later-coverage", "captured_material_id": captures[1], "artifact_ordinal": 0, "exact_scope": "The exact complete artifact.", "coverage": "complete_artifact", "lifecycle": "active"}, executive_epoch_id=epoch)
        self.assertEqual(covered["status"], "completed", covered)
        fresh = MissionInterface(
            store=self.store,
            cas=EvidenceCAS(self.store.paths),
            expected_mission_id="mission.1",
            canonical_snapshot=self.canonical,
        )
        second = self._query(
            epoch,
            "captures",
            page_size=1,
            cursor=first["next_cursor"],
            service=fresh,
        )
        self.assertEqual(second["items"][0]["id"], captures[1])
        self.assertEqual(second["items"][0]["pending_state"], "current_pending")
        refreshed = self._query(epoch, "captures", pending_state="fully_covered")
        self.assertEqual([item["id"] for item in refreshed["items"]], [captures[1]])

    def test_capture_continuation_preserves_as_of_coverage_after_last_coverer_removed(self):
        epoch = self._start()
        captures = [self._capture(epoch, f"covered-cut-{number}") for number in range(2)]
        authority = self.interface._direct_epoch_authority(epoch)
        self.assertIsNotNone(authority)
        assert authority is not None
        annotations = []
        for number, capture_handle in enumerate(captures):
            annotation = prepare_capture_scope_annotation(
                authority=authority,
                annotation_id=f"capture-annotation.covered-cut.{number}",
                capture_id=capture_handle.removeprefix("capture:"),
                exact_scope={
                    "artifact_ordinal": 0,
                    "coverage": "complete artifact",
                },
                lifecycle="active",
            )
            commit_capture_scope_annotation(
                self.store,
                record=annotation,
                lease=self.interface._writer_lease(),
                actor="coordinating-codex",
            )
            annotations.append(
                read_capture_scope_annotation(
                    self.store,
                    annotation_id=annotation.annotation_id,
                )
            )

        first = self._query(
            epoch,
            "captures",
            pending_state="fully_covered",
            page_size=1,
        )
        self.assertEqual([item["id"] for item in first["items"]], captures[:1])
        self.assertIsNotNone(first["next_cursor"])

        target = annotations[1]
        removed = prepare_capture_scope_annotation(
            authority=authority,
            annotation_id=target.annotation_id,
            capture_id=captures[1].removeprefix("capture:"),
            exact_scope={
                "artifact_ordinal": 0,
                "coverage": "complete artifact",
            },
            lifecycle="removed",
            expected_head_revision=target.revision,
            expected_head_payload_digest=target.payload_digest,
        )
        commit_capture_scope_annotation(
            self.store,
            record=removed,
            lease=self.interface._writer_lease(),
            actor="coordinating-codex",
        )

        fresh = MissionInterface(
            store=self.store,
            cas=EvidenceCAS(self.store.paths),
            expected_mission_id="mission.1",
            canonical_snapshot=self.canonical,
        )
        second = self._query(
            epoch,
            "captures",
            pending_state="fully_covered",
            page_size=1,
            cursor=first["next_cursor"],
            service=fresh,
        )
        self.assertEqual([item["id"] for item in second["items"]], captures[1:])
        self.assertEqual(second["items"][0]["pending_state"], "fully_covered")
        self.assertIsNone(second["next_cursor"])
        self.assertEqual(
            [
                item["id"]
                for item in self._query(
                    epoch,
                    "captures",
                    pending_state="fully_covered",
                )["items"]
            ],
            captures[:1],
        )
        self.assertIn(
            captures[1],
            {
                item["id"]
                for item in self._query(
                    epoch,
                    "captures",
                    pending_state="current_pending",
                )["items"]
            },
        )

    def test_capture_is_fully_covered_only_after_every_artifact_is_covered(self):
        epoch = self._start()
        blob = replace(
            self.cas.ingest_bytes(
                b"Two-artifact Capture coverage fixture.",
                original_name="two-artifact.txt",
                media_type="text/plain",
                encoding="utf-8",
            ).record,
            availability_state="verified_available",
        )
        capture_id = canonical_raw_capture_id(
            project_id="project.rh",
            capture_kind="output",
            observation_id="observation.coverage.two-artifact",
        )
        self.store.commit_raw_capture(
            record={
                "capture_id": capture_id,
                "mission_id": "mission.1",
                "executive_epoch_id": epoch,
                "capture_kind": "output",
                "observation_id": "observation.coverage.two-artifact",
                "assignment_id": "assignment.coverage.two-artifact",
                "provenance": {"kind": "direct_retrieval_test_fixture"},
                "completion": {"lifecycle": "completed"},
            },
            artifacts=tuple(
                {
                    "ordinal": ordinal,
                    "role": "result",
                    "logical_name": f"artifact-{ordinal}.txt",
                    "blob_sha256": blob.sha256,
                }
                for ordinal in (0, 1)
            ),
            blobs=(blob,),
            lease=self.interface._writer_lease(),
            command_id="capture.coverage.two-artifact",
            actor="coordinating-codex",
        )
        capture_handle = f"capture:{capture_id}"

        def selected(state: str) -> list[dict]:
            return self._query(
                epoch,
                "captures",
                query=capture_id,
                pending_state=state,
            )["items"]

        self.assertEqual([item["id"] for item in selected("current_pending")], [capture_handle])
        self.assertEqual(selected("fully_covered"), [])
        for ordinal in (0, 1):
            covered = self._execute(
                INTERPRET_MATERIAL,
                {
                    "judgment": "reviewed_no_current_semantic_delta",
                    "annotation_id": f"capture-annotation:two-artifact-{ordinal}",
                    "captured_material_id": capture_handle,
                    "artifact_ordinal": ordinal,
                    "exact_scope": f"The exact complete artifact {ordinal}.",
                    "coverage": "complete_artifact",
                    "lifecycle": "active",
                },
                executive_epoch_id=epoch,
            )
            self.assertEqual(covered["status"], "completed", covered)
            if ordinal == 0:
                self.assertEqual(
                    [item["id"] for item in selected("current_pending")],
                    [capture_handle],
                )
                self.assertEqual(selected("fully_covered"), [])

        self.assertEqual(selected("current_pending"), [])
        fully_covered = selected("fully_covered")
        self.assertEqual([item["id"] for item in fully_covered], [capture_handle])
        self.assertEqual(
            [artifact["pending"] for artifact in fully_covered[0]["artifacts"]],
            [False, False],
        )

        removed = self._execute(
            INTERPRET_MATERIAL,
            {
                "judgment": "reviewed_no_current_semantic_delta",
                "annotation_id": "capture-annotation:two-artifact-0",
                "captured_material_id": capture_handle,
                "artifact_ordinal": 0,
                "exact_scope": "The exact complete artifact 0.",
                "coverage": "complete_artifact",
                "lifecycle": "removed",
            },
            executive_epoch_id=epoch,
        )
        self.assertEqual(removed["status"], "completed", removed)
        partial = selected("current_pending")
        self.assertEqual([item["id"] for item in partial], [capture_handle])
        self.assertEqual(
            [artifact["pending"] for artifact in partial[0]["artifacts"]],
            [True, False],
        )
        self.assertEqual(selected("fully_covered"), [])

    def test_capture_retrieval_rejects_an_absent_selected_epoch_chain(self):
        source_epoch = self._start()
        capture_handle = self._capture(source_epoch, "missing-epoch-chain")
        self._fail(source_epoch)
        active_epoch = self._successor("after-missing-capture-epoch")
        cut_project_commit = int(
            self.store.read_metadata()["current_project_commit"]
        )
        with closing(sqlite3.connect(self.store.paths.database)) as connection:
            connection.execute(
                "DELETE FROM executive_epoch_event WHERE executive_epoch_id = ?",
                (source_epoch,),
            )
            connection.commit()
        with self.assertRaisesRegex(
            WorkspaceIntegrityError,
            "journal reference has no persisted row|prior bound Executive Epoch",
        ):
            with self.store.direct_recovery_read_scope():
                self.store.read_direct_recovery_facts(
                    cut_project_commit=cut_project_commit
                )
        response = self.interface.execute_semantic_operation(
            self._request(
                RETRIEVE,
                {
                    "mode": "captures",
                    "purpose": "Reject a Capture whose exact Epoch was removed.",
                    "query": capture_handle.removeprefix("capture:"),
                },
            ),
            executive_epoch_id=active_epoch,
            root_query_context=self.key,
        )
        self.assertEqual(response["status"], "unavailable", response)
        self.assertRegex(
            response["error"]["message"],
            "exact origin|prior (?:bound )?Executive Epoch",
        )

    def test_capture_change_rejects_an_absent_selected_epoch_chain(self):
        checkpoint_epoch = self._start()
        self._capture_pending_material(checkpoint_epoch)
        checkpoint = self._execute(
            CHECKPOINT,
            {},
            executive_epoch_id=checkpoint_epoch,
        )
        self.assertEqual(checkpoint["status"], "completed", checkpoint)
        checkpoint_id = checkpoint["result"]["checkpoint_id"]
        source_epoch = self._successor("capture-change-source")
        self._capture(source_epoch, "capture-change-missing-epoch")
        self._fail(source_epoch)
        active_epoch = self._successor("capture-change-query")
        with closing(sqlite3.connect(self.store.paths.database)) as connection:
            connection.execute(
                "DELETE FROM executive_epoch_event WHERE executive_epoch_id = ?",
                (source_epoch,),
            )
            connection.commit()
        response = self.interface.execute_semantic_operation(
            self._request(
                RETRIEVE,
                {
                    "mode": "changes_since_checkpoint",
                    "purpose": "Reject a changed Capture whose Epoch was removed.",
                    "checkpoint_id": checkpoint_id,
                    "kinds": ["capture"],
                    "relations": ["new"],
                },
            ),
            executive_epoch_id=active_epoch,
            root_query_context=self.key,
        )
        self.assertEqual(response["status"], "unavailable", response)
        self.assertRegex(
            response["error"]["message"],
            "exact origin|prior (?:bound )?Executive Epoch",
        )

    def test_raw_capture_writer_rejects_an_authorized_but_unbound_epoch(self):
        epoch = self._authorize()
        blob = replace(
            EvidenceCAS(self.store.paths).ingest_bytes(
                b"An unbound epoch cannot own this synthetic result.",
                original_name="unbound-result.txt",
                media_type="text/plain",
                encoding="utf-8",
            ).record,
            availability_state="verified_available",
        )
        observation_id = "observation.unbound-epoch"
        capture_id = canonical_raw_capture_id(
            project_id="project.rh",
            capture_kind="output",
            observation_id=observation_id,
        )
        before = deep_thaw(self.store.read_metadata())
        with self.assertRaisesRegex(
            StaleCommandError,
            "was not bound before capture",
        ):
            self.store.commit_raw_capture(
                record={
                    "capture_id": capture_id,
                    "mission_id": "mission.1",
                    "executive_epoch_id": epoch,
                    "capture_kind": "output",
                    "observation_id": observation_id,
                    "assignment_id": "assignment.unbound-epoch",
                    "provenance": {"kind": "synthetic_unbound_epoch_fixture"},
                    "completion": {"lifecycle": "completed"},
                },
                artifacts=(
                    {
                        "ordinal": 0,
                        "role": "result",
                        "logical_name": "unbound-result.txt",
                        "blob_sha256": blob.sha256,
                    },
                ),
                blobs=(blob,),
                lease=self.interface._writer_lease(),
                command_id="capture.unbound-epoch",
                actor="coordinating-codex",
            )
        self.assertEqual(deep_thaw(self.store.read_metadata()), before)

    def test_capture_epoch_binding_rejects_nonmonotonic_terminal_history(self):
        impossible_chain = (
            {
                "mission_id": "mission.1",
                "event_kind": "authorized",
                "project_commit_no": 10,
            },
            {
                "mission_id": "mission.1",
                "event_kind": "bound",
                "project_commit_no": 20,
            },
            {
                "mission_id": "mission.1",
                "event_kind": "failed_before_checkpoint",
                "project_commit_no": 15,
            },
        )
        with (
            closing(sqlite3.connect(":memory:")) as connection,
            mock.patch.object(
                store_module,
                "_read_executive_epoch_events_from_connection",
                return_value=impossible_chain,
            ),
            mock.patch.object(
                store_module,
                "_require_root_retrieval_epoch_chain_origins",
            ),
            self.assertRaisesRegex(
                WorkspaceIntegrityError,
                "prior bound Executive Epoch",
            ),
        ):
            store_module._require_raw_capture_epoch_before_origin(
                connection,
                project_id="project.rh",
                mission_id="mission.1",
                executive_epoch_id="epoch.impossible",
                origin_commit=25,
                stale=False,
            )

    def test_exact_capture_read_and_replay_require_the_prior_bound_epoch(self):
        epoch = self._start()
        blob = replace(
            EvidenceCAS(self.store.paths).ingest_bytes(
                b"Exact replay must retain its bound Epoch lineage.",
                original_name="bound-replay.txt",
                media_type="text/plain",
                encoding="utf-8",
            ).record,
            availability_state="verified_available",
        )
        observation_id = "observation.bound-replay"
        capture_id = canonical_raw_capture_id(
            project_id="project.rh",
            capture_kind="output",
            observation_id=observation_id,
        )
        record = {
            "capture_id": capture_id,
            "mission_id": "mission.1",
            "executive_epoch_id": epoch,
            "capture_kind": "output",
            "observation_id": observation_id,
            "assignment_id": "assignment.bound-replay",
            "provenance": {"kind": "synthetic_bound_replay_fixture"},
            "completion": {"lifecycle": "completed"},
        }
        artifacts = (
            {
                "ordinal": 0,
                "role": "result",
                "logical_name": "bound-replay.txt",
                "blob_sha256": blob.sha256,
            },
        )
        command_id = "capture.bound-replay"
        self.store.commit_raw_capture(
            record=record,
            artifacts=artifacts,
            blobs=(blob,),
            lease=self.interface._writer_lease(),
            command_id=command_id,
            actor="coordinating-codex",
        )
        with closing(sqlite3.connect(self.store.paths.database)) as connection:
            connection.execute(
                "DELETE FROM executive_epoch_event WHERE executive_epoch_id = ?",
                (epoch,),
            )
            connection.commit()

        with self.assertRaisesRegex(
            WorkspaceIntegrityError,
            "prior bound Executive Epoch",
        ):
            self.store.read_raw_capture(capture_id)
        before_replay = deep_thaw(self.store.read_metadata())
        with self.assertRaisesRegex(
            WorkspaceIntegrityError,
            "prior bound Executive Epoch",
        ):
            self.store.commit_raw_capture(
                record=record,
                artifacts=artifacts,
                blobs=(blob,),
                lease=self.interface._writer_lease(),
                command_id=command_id,
                actor="coordinating-codex",
            )
        with self.assertRaisesRegex(
            WorkspaceIntegrityError,
            "prior bound Executive Epoch",
        ):
            self.store.commit_raw_capture(
                record=record,
                artifacts=artifacts,
                blobs=(blob,),
                lease=self.interface._writer_lease(),
                command_id=f"{command_id}.semantic-noop",
                actor="coordinating-codex",
            )
        self.assertEqual(deep_thaw(self.store.read_metadata()), before_replay)

    def test_raw_capture_semantic_noop_authenticates_its_historical_origin(self):
        epoch = self._start()
        blob = replace(
            EvidenceCAS(self.store.paths).ingest_bytes(
                b"A semantic no-op still depends on exact historical custody.",
                original_name="capture-noop-origin.txt",
                media_type="text/plain",
                encoding="utf-8",
            ).record,
            availability_state="verified_available",
        )
        observation_id = "observation.capture-noop-origin"
        capture_id = canonical_raw_capture_id(
            project_id="project.rh",
            capture_kind="output",
            observation_id=observation_id,
        )
        record = {
            "capture_id": capture_id,
            "mission_id": "mission.1",
            "executive_epoch_id": epoch,
            "capture_kind": "output",
            "observation_id": observation_id,
            "assignment_id": "assignment.capture-noop-origin",
            "provenance": {"kind": "semantic_noop_origin_fixture"},
            "completion": {"lifecycle": "completed"},
        }
        artifacts = (
            {
                "ordinal": 0,
                "role": "result",
                "logical_name": "capture-noop-origin.txt",
                "blob_sha256": blob.sha256,
            },
        )
        created = self.store.commit_raw_capture(
            record=record,
            artifacts=artifacts,
            blobs=(blob,),
            lease=self.interface._writer_lease(),
            command_id="capture.noop-origin.created",
            actor="coordinating-codex",
        )
        self._capture(epoch, "capture-noop-origin-later")
        before_healthy_noop = deep_thaw(self.store.read_metadata())
        healthy_noop_command = "capture.noop-origin.healthy-different-command"
        healthy_noop = self.store.commit_raw_capture(
            record=record,
            artifacts=artifacts,
            blobs=(blob,),
            lease=self.interface._writer_lease(),
            command_id=healthy_noop_command,
            actor="coordinating-codex",
        )
        self.assertTrue(healthy_noop.replayed)
        self.assertTrue(healthy_noop.result["semantic_noop"])
        self.assertEqual(
            deep_thaw(self.store.read_metadata()),
            before_healthy_noop,
        )
        with closing(sqlite3.connect(self.store.paths.database)) as connection:
            self.assertEqual(
                int(
                    connection.execute(
                        "SELECT COUNT(*) FROM command_result WHERE command_id = ?",
                        (healthy_noop_command,),
                    ).fetchone()[0]
                ),
                0,
            )
            stored_writes = json.loads(
                str(
                    connection.execute(
                        "SELECT auxiliary_writes_json FROM transition_journal "
                        "WHERE project_commit_no = ?",
                        (created.project_commit,),
                    ).fetchone()[0]
                )
            )
            next(
                item for item in stored_writes if item["table"] == "raw_capture"
            )["row_digest"] = "f" * 64
            connection.execute(
                "UPDATE transition_journal SET auxiliary_writes_json = ? "
                "WHERE project_commit_no = ?",
                (
                    canonical_json_bytes(stored_writes).decode("utf-8"),
                    created.project_commit,
                ),
            )
            connection.commit()

        before_noop = deep_thaw(self.store.read_metadata())
        failed_noop_command = "capture.noop-origin.after-corruption"
        with self.assertRaises(WorkspaceIntegrityError):
            self.store.commit_raw_capture(
                record=record,
                artifacts=artifacts,
                blobs=(blob,),
                lease=self.interface._writer_lease(),
                command_id=failed_noop_command,
                actor="coordinating-codex",
            )
        self.assertEqual(deep_thaw(self.store.read_metadata()), before_noop)
        with closing(sqlite3.connect(self.store.paths.database)) as connection:
            self.assertEqual(
                int(
                    connection.execute(
                        "SELECT COUNT(*) FROM command_result WHERE command_id = ?",
                        (failed_noop_command,),
                    ).fetchone()[0]
                ),
                0,
            )

    def test_capture_dependent_reads_writes_noops_and_replays_require_exact_custody(self):
        source_epoch = self._start()
        capture_id = self._capture(
            source_epoch,
            "dependent-custody",
        ).removeprefix("capture:")
        self._fail(source_epoch)
        active_epoch = self._successor("dependent-custody-active")
        replacement_capture_id = self._capture(
            active_epoch,
            "dependent-custody-replacement",
        ).removeprefix("capture:")
        authority = self.interface._direct_epoch_authority(active_epoch)
        self.assertIsNotNone(authority)
        assert authority is not None
        scope = {
            "artifact_ordinal": 0,
            "coverage": "complete artifact",
            "description": "The exact Capture artifact under test.",
        }
        annotation = prepare_capture_scope_annotation(
            authority=authority,
            annotation_id="dependent-custody",
            capture_id=capture_id,
            exact_scope=scope,
            lifecycle="active",
        )
        annotation_command = "capture-annotation.dependent-custody"
        commit_capture_scope_annotation(
            self.store,
            record=annotation,
            lease=self.interface._writer_lease(),
            actor="coordinating-codex",
            command_id=annotation_command,
        )
        evidence = prepare_evidence_meaning(
            authority=authority,
            evidence_id="dependent-custody",
            statement="One exact Capture is retained for the custody regression.",
            exact_scope="The exact Capture artifact only.",
            strength="heuristic",
            semantic_role="result",
            authority_basis="current executive interpretation",
            sources=(
                CaptureScope(
                    capture_id=capture_id,
                    artifact_ordinal=0,
                    exact_scope={
                        "description": "The exact Capture artifact under test.",
                        "coverage": "complete artifact",
                    },
                ),
            ),
            non_inferences=("No general result follows from this fixture.",),
            limitations=("This is a custody regression fixture.",),
        )
        evidence_command = "evidence.dependent-custody"
        commit_evidence_meaning(
            self.store,
            record=evidence,
            lease=self.interface._writer_lease(),
            actor="coordinating-codex",
            command_id=evidence_command,
        )
        with closing(sqlite3.connect(self.store.paths.database)) as connection:
            connection.execute(
                "DELETE FROM executive_epoch_event WHERE executive_epoch_id = ?",
                (source_epoch,),
            )
            connection.commit()
        before = deep_thaw(self.store.read_metadata())

        with self.assertRaisesRegex(
            WorkspaceIntegrityError,
            "prior bound Executive Epoch|journal reference has no persisted row",
        ):
            self.store.read_capture_scope_annotation("dependent-custody")
        with self.assertRaisesRegex(
            WorkspaceIntegrityError,
            "prior bound Executive Epoch|journal reference has no persisted row",
        ):
            self.store.list_mission_capture_scope_annotation_heads("mission.1")
        with self.assertRaisesRegex(
            WorkspaceIntegrityError,
            "prior bound Executive Epoch|journal reference has no persisted row",
        ):
            with self.store.direct_recovery_read_scope():
                self.store.read_root_owner_at_cut(
                    kind="evidence",
                    identity=evidence.evidence.evidence_id,
                    cut_project_commit=int(before["current_project_commit"]),
                )
        for record, command_id, commit in (
            (annotation, annotation_command, commit_capture_scope_annotation),
            (evidence, evidence_command, commit_evidence_meaning),
        ):
            with self.subTest(replay=command_id), self.assertRaisesRegex(
                WorkspaceIntegrityError,
                "prior bound Executive Epoch|journal reference has no persisted row",
            ):
                commit(
                    self.store,
                    record=record,
                    lease=self.interface._writer_lease(),
                    actor="coordinating-codex",
                    command_id=command_id,
                )
            with self.subTest(noop=command_id), self.assertRaisesRegex(
                WorkspaceIntegrityError,
                "prior bound Executive Epoch|journal reference has no persisted row",
            ):
                commit(
                    self.store,
                    record=record,
                    lease=self.interface._writer_lease(),
                    actor="coordinating-codex",
                    command_id=f"{command_id}.semantic-noop",
                )

        new_annotation = prepare_capture_scope_annotation(
            authority=authority,
            annotation_id="dependent-custody-new",
            capture_id=capture_id,
            exact_scope=scope,
            lifecycle="active",
        )
        new_evidence = prepare_evidence_meaning(
            authority=authority,
            evidence_id="dependent-custody-new",
            statement="A new interpretation cannot rely on corrupted custody.",
            exact_scope="The exact Capture artifact only.",
            strength="heuristic",
            semantic_role="result",
            authority_basis="current executive interpretation",
            sources=(
                CaptureScope(
                    capture_id=capture_id,
                    artifact_ordinal=0,
                    exact_scope={
                        "description": "The exact Capture artifact under test.",
                        "coverage": "complete artifact",
                    },
                ),
            ),
            non_inferences=("No general result follows from this fixture.",),
            limitations=("This is a custody regression fixture.",),
        )
        for record, command_id, commit in (
            (
                new_annotation,
                "capture-annotation.dependent-custody-new",
                commit_capture_scope_annotation,
            ),
            (
                new_evidence,
                "evidence.dependent-custody-new",
                commit_evidence_meaning,
            ),
        ):
            with self.subTest(new_write=command_id), self.assertRaisesRegex(
                WorkspaceIntegrityError,
                "prior bound Executive Epoch|journal reference has no persisted row",
            ):
                commit(
                    self.store,
                    record=record,
                    lease=self.interface._writer_lease(),
                    actor="coordinating-codex",
                    command_id=command_id,
                )

        moved_evidence = prepare_evidence_meaning(
            authority=authority,
            evidence_id=evidence.evidence.evidence_id,
            statement="The replacement Capture would supersede the first source.",
            exact_scope="The exact replacement Capture artifact only.",
            strength="heuristic",
            semantic_role="result",
            authority_basis="current executive interpretation",
            sources=(
                CaptureScope(
                    capture_id=replacement_capture_id,
                    artifact_ordinal=0,
                    exact_scope={
                        "description": "The exact replacement Capture artifact.",
                        "coverage": "complete artifact",
                    },
                ),
            ),
            non_inferences=("No general result follows from this fixture.",),
            limitations=("This is a custody regression fixture.",),
            expected_head_revision=evidence.evidence.revision,
            expected_head_payload_digest=evidence.payload_digest,
        )
        for record, command_id, commit in (
            (
                moved_evidence,
                "evidence.dependent-custody-move",
                commit_evidence_meaning,
            ),
        ):
            with self.subTest(move=command_id), self.assertRaisesRegex(
                WorkspaceIntegrityError,
                "prior bound Executive Epoch|journal reference has no persisted row",
            ):
                commit(
                    self.store,
                    record=record,
                    lease=self.interface._writer_lease(),
                    actor="coordinating-codex",
                    command_id=command_id,
                )
        self.assertEqual(deep_thaw(self.store.read_metadata()), before)

    def test_old_evidence_and_annotation_replays_authenticate_the_original_effect(self):
        epoch = self._start()
        first_capture_id = self._capture(
            epoch,
            "old-replay-effect-first",
        ).removeprefix("capture:")
        second_capture_id = self._capture(
            epoch,
            "old-replay-effect-second",
        ).removeprefix("capture:")
        authority = self.interface._direct_epoch_authority(epoch)
        self.assertIsNotNone(authority)
        assert authority is not None

        def evidence_revision(
            capture_id: str,
            *,
            expected_revision: int | None = None,
            expected_digest: str | None = None,
        ):
            return prepare_evidence_meaning(
                authority=authority,
                evidence_id="old-replay-effect",
                statement=f"Exact replay source is {capture_id}.",
                exact_scope="One exact Capture artifact.",
                strength="heuristic",
                semantic_role="result",
                authority_basis="current executive interpretation",
                sources=(
                    CaptureScope(
                        capture_id=capture_id,
                        artifact_ordinal=0,
                        exact_scope={
                            "description": "The exact replay Capture artifact.",
                            "coverage": "complete artifact",
                        },
                    ),
                ),
                non_inferences=("No broader result follows.",),
                limitations=("Replay-effect custody fixture only.",),
                expected_head_revision=expected_revision,
                expected_head_payload_digest=expected_digest,
            )

        first_evidence = evidence_revision(first_capture_id)
        evidence_command = "evidence.old-replay-effect"
        first_evidence_outcome = commit_evidence_meaning(
            self.store,
            record=first_evidence,
            lease=self.interface._writer_lease(),
            actor="coordinating-codex",
            command_id=evidence_command,
        )
        second_evidence = evidence_revision(
            second_capture_id,
            expected_revision=first_evidence.evidence.revision,
            expected_digest=first_evidence.payload_digest,
        )
        commit_evidence_meaning(
            self.store,
            record=second_evidence,
            lease=self.interface._writer_lease(),
            actor="coordinating-codex",
            command_id="evidence.old-replay-effect.advance",
        )
        before_evidence_replay = deep_thaw(self.store.read_metadata())
        evidence_replay = commit_evidence_meaning(
            self.store,
            record=first_evidence,
            lease=self.interface._writer_lease(),
            actor="coordinating-codex",
            command_id=evidence_command,
        )
        self.assertTrue(evidence_replay.replayed)
        self.assertEqual(
            evidence_replay.project_commit,
            first_evidence_outcome.project_commit,
        )
        self.assertEqual(
            deep_thaw(self.store.read_metadata()),
            before_evidence_replay,
        )
        self.assertEqual(
            self.store.read_evidence_meaning_revision(
                first_evidence.evidence.evidence_id
            )["record"]["revision"],
            2,
        )

        annotation_scope = {
            "artifact_ordinal": 0,
            "coverage": "complete artifact",
            "description": "The exact replay annotation scope.",
        }
        first_annotation = prepare_capture_scope_annotation(
            authority=authority,
            annotation_id="old-replay-effect",
            capture_id=first_capture_id,
            exact_scope=annotation_scope,
            lifecycle="active",
        )
        annotation_command = "capture-annotation.old-replay-effect"
        first_annotation_outcome = commit_capture_scope_annotation(
            self.store,
            record=first_annotation,
            lease=self.interface._writer_lease(),
            actor="coordinating-codex",
            command_id=annotation_command,
        )
        second_annotation = prepare_capture_scope_annotation(
            authority=authority,
            annotation_id="old-replay-effect",
            capture_id=first_capture_id,
            exact_scope=annotation_scope,
            lifecycle="removed",
            expected_head_revision=first_annotation.revision,
            expected_head_payload_digest=first_annotation.payload_digest,
        )
        commit_capture_scope_annotation(
            self.store,
            record=second_annotation,
            lease=self.interface._writer_lease(),
            actor="coordinating-codex",
            command_id="capture-annotation.old-replay-effect.advance",
        )
        before_annotation_replay = deep_thaw(self.store.read_metadata())
        annotation_replay = commit_capture_scope_annotation(
            self.store,
            record=first_annotation,
            lease=self.interface._writer_lease(),
            actor="coordinating-codex",
            command_id=annotation_command,
        )
        self.assertTrue(annotation_replay.replayed)
        self.assertEqual(
            annotation_replay.project_commit,
            first_annotation_outcome.project_commit,
        )
        self.assertEqual(
            deep_thaw(self.store.read_metadata()),
            before_annotation_replay,
        )
        self.assertEqual(
            self.store.read_capture_scope_annotation(
                first_annotation.annotation_id
            )["revision"],
            2,
        )

        with closing(sqlite3.connect(self.store.paths.database)) as connection:
            connection.execute(
                "DELETE FROM evidence_capture_source "
                "WHERE evidence_id = ? AND evidence_revision = 1",
                (first_evidence.evidence.evidence_id,),
            )
            connection.execute(
                "DELETE FROM capture_scope_annotation_revision "
                "WHERE annotation_id = ? AND revision = 1",
                (first_annotation.annotation_id,),
            )
            connection.commit()
        before_failed_replays = deep_thaw(self.store.read_metadata())
        for record, command_id, commit in (
            (first_evidence, evidence_command, commit_evidence_meaning),
            (first_annotation, annotation_command, commit_capture_scope_annotation),
        ):
            with self.subTest(command_id=command_id), self.assertRaises(
                WorkspaceIntegrityError
            ):
                commit(
                    self.store,
                    record=record,
                    lease=self.interface._writer_lease(),
                    actor="coordinating-codex",
                    command_id=command_id,
                )
            self.assertEqual(
                deep_thaw(self.store.read_metadata()),
                before_failed_replays,
            )
        self.assertEqual(
            self.store.read_evidence_meaning_revision(
                first_evidence.evidence.evidence_id
            )["record"]["revision"],
            2,
        )
        self.assertEqual(
            self.store.read_capture_scope_annotation(
                first_annotation.annotation_id
            )["revision"],
            2,
        )

    def test_direct_recovery_rejects_an_unjournaled_capture_artifact(self):
        epoch = self._start()
        capture_id = self._capture(
            epoch,
            "extra-artifact",
        ).removeprefix("capture:")
        cut_project_commit = int(
            self.store.read_metadata()["current_project_commit"]
        )
        with closing(sqlite3.connect(self.store.paths.database)) as connection:
            artifact = connection.execute(
                "SELECT role, logical_name, blob_sha256 "
                "FROM raw_capture_artifact WHERE capture_id = ? AND ordinal = 0",
                (capture_id,),
            ).fetchone()
            self.assertIsNotNone(artifact)
            assert artifact is not None
            connection.execute(
                "INSERT INTO raw_capture_artifact("
                "capture_id, ordinal, role, logical_name, blob_sha256"
                ") VALUES (?, 1, ?, ?, ?)",
                (
                    capture_id,
                    str(artifact[0]),
                    f"extra-{artifact[1]}",
                    str(artifact[2]),
                ),
            )
            connection.commit()
        with self.assertRaisesRegex(
            WorkspaceIntegrityError,
            "artifact inventory differs from authoritative journal history",
        ):
            with self.store.direct_recovery_read_scope():
                self.store.read_direct_recovery_facts(
                    cut_project_commit=cut_project_commit
                )

    def test_proof_projection_cursor_ignores_unrelated_writes_but_rejects_changed_attention(self):
        epoch = self._start()
        # This test isolates cursor behavior; actual A1 lifecycle/owner binding
        # fixtures are exercised in test_executive_orientation.py.
        proof = {"open_candidate_a1": [{"candidate_ref": {"id": f"candidate:cursor-{number}", "revision": 1, "payload_sha256": "ab" * 32}, "retrieval_handle": f"candidate:cursor-{number}@1", "claim_disposition": "proof", "stage": "awaiting_a1_review", "triage": None, "admission": None} for number in range(2)], "admitted_result": None}
        with (
            mock.patch(
                "research_core.executive_orientation.proof_attention_in_snapshot",
                return_value=proof,
            ),
            mock.patch.object(
                self.store,
                "read_direct_recovery_facts",
                side_effect=AssertionError(
                    "proof-attention retrieval reconstructed retained history"
                ),
            ),
        ):
            first = self._query(epoch, "proof_attention", page_size=1)
            self._capture(epoch, "proof-unrelated")
            second = self._query(epoch, "proof_attention", page_size=1, cursor=first["next_cursor"])
            self.assertEqual(second["scope"]["basis"], "live_proof_attention")
            self.assertEqual(second["items"], proof["open_candidate_a1"][1:])
            proof["open_candidate_a1"] = proof["open_candidate_a1"][1:]
            rejected = self.interface.execute_semantic_operation(self._request(RETRIEVE, {"mode": "proof_attention", "purpose": "Deliberate test selection.", "page_size": 1, "cursor": first["next_cursor"]}), executive_epoch_id=epoch, root_query_context=self.key)
            self.assertEqual(rejected["error"]["code"], "mission_retrieval_cursor_invalid")


class RootRetrievalContractTests(unittest.TestCase):
    def test_exhaustive_compatibility_changes_include_capture_and_annotation(self):
        from research_core.mission_retrieval import change_descriptors_in_snapshot

        facts = {
            "observed_project_commit": 6,
            "head_history": [],
            "retained_history": [
                {
                    "source_family": "capture_annotations",
                    "identity": "annotation.legacy",
                    "revision": 1,
                    "project_commit": 3,
                },
                {
                    "source_family": "capture_annotations",
                    "identity": "annotation.legacy",
                    "revision": 2,
                    "project_commit": 5,
                },
            ],
            "raw_capture_origins": [
                {
                    "capture_id": "capture.legacy",
                    "mission_id": "mission.1",
                    "project_commit": 4,
                },
                {
                    "capture_id": "capture.foreign",
                    "mission_id": "mission.other",
                    "project_commit": 4,
                },
            ],
            "post_cut_transitions": [],
        }
        service = SimpleNamespace(_mission_id="mission.1")
        with mock.patch(
            "research_core.mission_retrieval._owner",
            return_value={"document": {}, "reference": {}},
        ):
            changes = change_descriptors_in_snapshot(
                service,
                facts,
                baseline=2,
                cut=6,
            )
        self.assertEqual(
            changes,
            [
                {
                    "kind": "capture",
                    "id": "capture:capture.legacy",
                    "before_handle": None,
                    "current_handle": "capture:capture.legacy",
                    "changes": ["new"],
                },
                {
                    "kind": "capture-annotation",
                    "id": "capture-annotation:annotation.legacy",
                    "before_handle": None,
                    "current_handle": "capture-annotation:annotation.legacy@2",
                    "changes": ["new", "advanced"],
                },
            ],
        )

    def test_validated_fact_classification_keeps_created_then_retired_identity(self):
        from research_core.mission_retrieval import _owners, change_descriptors_in_snapshot
        facts = {"observed_project_commit": 4, "head_history": [], "retained_history": [
            {"source_family": "branches", "identity": "gone", "revision": 1, "project_commit": 2},
            {"source_family": "branches", "identity": "gone", "revision": 2, "project_commit": 3},
        ], "post_cut_transitions": [
            {"project_commit": 2, "head_changes": [{"kind": "branch", "identity": "gone", "revision": 1}], "evidence_head_advances": [], "retired_heads": []},
            {"project_commit": 3, "head_changes": [{"kind": "branch", "identity": "gone", "revision": 2}], "evidence_head_advances": [], "retired_heads": []},
            {"project_commit": 4, "head_changes": [], "evidence_head_advances": [], "retired_heads": [{"kind": "branch", "object_id": "gone", "revision": 2}]},
        ]}
        # This supplies the already-validated Store fact interface; it does not
        # fabricate a writable Mission retirement command or claim migration
        # qualification. Existing Store journal validation remains authoritative.
        with mock.patch("research_core.mission_retrieval._owner", return_value={"mission_id": "mission.1"}):
            result = change_descriptors_in_snapshot(object(), facts, baseline=1, cut=4)
        self.assertEqual(result, [{"kind": "branch", "id": "branch:gone", "before_handle": None, "current_handle": None, "changes": ["new", "advanced", "retired"]}])
        # Internal preservation records remain outside ordinary discovery and
        # change counts, just as in the existing public reconstruction.
        internal = {"mission_id": "mission.1", "recovery_internal": "candidate_a1_preservation"}
        with mock.patch("research_core.mission_retrieval._owner", return_value=internal):
            self.assertEqual(change_descriptors_in_snapshot(object(), facts, baseline=1, cut=4), [])
            self.assertEqual(list(_owners(object(), facts, 3, {"branch"})), [])

    def test_all_usage_examples_validate_and_specialist_request_modes_do_not_expand(self):
        guide = deep_thaw(project_model_usage(for_operation=RETRIEVE))
        modes = {item["properties"]["mode"]["const"] for item in guide["input_schema"]["oneOf"]}
        self.assertEqual(modes, set(ROOT_RETRIEVE_MODES))
        # Existing owner projects validated call examples; validate them again
        # here through the actual model request parser, not JSON-schema alone.
        for example in guide["examples"]:
            call = example["call"]
            validate_semantic_request(fixtures.DirectMissionInterfaceTests._request(call["operation"], call["input"]))
        for mode in set(ROOT_RETRIEVE_MODES) - {"read", "search"}:
            selection = {"mode": mode, "purpose": "Root-only bounded discovery."}
            if mode == "checkpoint":
                selection["section"] = "summary"
            elif mode == "publication_result":
                selection = {"mode": mode, "command_id": "scientific-publication.exact"}
            with self.assertRaises(MissionOperationContractError):
                validate_historical_read_request(fixtures.DirectMissionInterfaceTests._request(RETRIEVE, selection))


if __name__ == "__main__":
    unittest.main(
        argv=[
            argument
            for argument in sys.argv
            if argument != _ROOT6_PUBLIC_RETRIEVAL_SCALE_FLAG
        ]
    )
