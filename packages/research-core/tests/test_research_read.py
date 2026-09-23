"""Provider-free ordinary research-read checks using the real small Store fixture."""
from __future__ import annotations

import json
import unittest
from unittest import mock

import test_mission_interface_direct as fixtures
from research_core.mission_interface import MissionInterfaceError
from research_core.mission_operation_contract import RECORD_CANDIDATE, RECORD_CONTEXT, RECORD_STRATEGY
from research_core.research_model import deep_thaw


class ResearchReadTests(unittest.TestCase):
    setUpClass = classmethod(fixtures.DirectMissionInterfaceTests.setUpClass.__func__)
    setUp = fixtures.DirectMissionInterfaceTests.setUp
    _request = staticmethod(fixtures.DirectMissionInterfaceTests._request)
    _execute = fixtures.DirectMissionInterfaceTests._execute
    _authorize = fixtures.DirectMissionInterfaceTests._authorize
    _bind = fixtures.DirectMissionInterfaceTests._bind
    key = {"cursor_mac_key": "de" * 32}

    def _start(self, families=("branches", "candidates", "contexts", "evidence", "strategies")):
        self.epoch = self._authorize()
        self._bind(self.epoch)
        self.binding = {
            "project_id": "project.rh", "mission_id": "mission.1", "executive_epoch_id": self.epoch,
            "root_thread_id": self.goal_thread_id, "actual_child_thread_id": "child:research",
            "actual_parent_thread_id": self.goal_thread_id, "actual_depth": 1,
        }
        self.grant = self.interface.issue_research_read_grant({
            "child_thread_id": "child:research", "assignment": "Resolve one finite normalization question and seek relevant retained sources.",
            "source_families": list(families), "raw_body_policy": "metadata_only",
        }, binding=self.binding)
        self.execution = {**self.binding, "expected_grant_id": self.grant["grant_id"],
                          "expected_assignment_id": self.grant["assignment_id"]}

    def _read(self, mode, *, key=None, **selection):
        return deep_thaw(self.interface.execute_research_read({
            "mode": "retrieve", "selection": {"mode": mode, "purpose": "Resolve the assigned question.", **selection},
        }, grant=self.grant, binding=self.execution, research_query_context=key or self.key))

    def _candidate(self, identity):
        result = self._execute(RECORD_CANDIDATE, {
            "candidate_id": "candidate:" + identity, "proposal_kind": "lemma",
            "exact_statement": "research-read-page-marker: finite diagnostic " + identity,
            "standing": {"status": "open", "basis": "An unverified finite fixture."},
        }, executive_epoch_id=self.epoch)
        self.assertEqual(result["status"], "completed", result)

    def test_ordinary_assignment_needs_no_advisory_context_and_reads_without_writers(self):
        self._start()
        self.assertNotIn("context", self.grant)
        self.assertNotIn("project_commit_cut", self.grant)
        before = deep_thaw(self.store.read_metadata())
        with mock.patch.object(self.interface, "_writer_lease", side_effect=AssertionError("research read acquired writer")), \
             mock.patch.object(self.interface, "execute_semantic_operation", side_effect=AssertionError("researcher impersonated root")):
            usage = self.interface.execute_research_read({"mode": "usage", "topics": ["sources"]},
                grant=self.grant, binding=self.execution, research_query_context=self.key)
            self.assertEqual(set(usage["result"]["topics"]), {"sources"})
            result = self._read("read", ids=["strategy:strategy.theta.1"])
            self.assertEqual(result["result"]["items"][0]["status"], "readable")
            history = self._read("history_inventory", source_families=["branches"], page_size=1)
            self.assertEqual(history["result"]["project_commit_cut"], history["project_commit_cut"])
        self.assertEqual(before, deep_thaw(self.store.read_metadata()))

    def test_new_queries_advance_but_search_continuation_keeps_original_cut(self):
        self._start(("candidates",))
        self._candidate("read-a")
        self._candidate("read-b")
        first = self._read("search", query="research-read-page-marker", page_size=1)
        cursor = first["result"]["next_cursor"]
        self.assertIsNotNone(cursor)
        self._candidate("read-c")
        second = self._read("search", query="research-read-page-marker", page_size=1, cursor=cursor)
        self.assertEqual(first["project_commit_cut"], second["project_commit_cut"])
        self.assertIsNone(second["result"]["next_cursor"])
        self.assertNotIn("candidate:read-c@1", [item["id"] for item in second["result"]["items"]])
        fresh = self._read("search", query="research-read-page-marker", page_size=50)
        self.assertGreater(fresh["project_commit_cut"], first["project_commit_cut"])
        self.assertEqual(len(fresh["result"]["items"]), 3)
        with self.assertRaisesRegex(MissionInterfaceError, "continuation"):
            self._read("search", query="research-read-page-marker", page_size=1, cursor=cursor,
                       key={"cursor_mac_key": "ab" * 32})
        with self.assertRaises(MissionInterfaceError):
            self._read("search", query="different question", page_size=1, cursor=cursor)

    def test_exact_historical_revision_does_not_float_with_new_strategy(self):
        self._start()
        original = self._read("read", ids=["strategy:strategy.theta.1@1"])
        response = self._execute(RECORD_STRATEGY, {
            "mission_continuation": "continue", "integrated_comparison": "A later exact fixture decision.",
            "reconsideration_conditions": [{"condition": "Reconsider on new mathematical evidence."}],
        }, executive_epoch_id=self.epoch)
        self.assertEqual(response["status"], "completed", response)
        later = self._read("read", ids=["strategy:strategy.theta.1@1"])
        self.assertEqual(original["result"]["items"], later["result"]["items"])
        self.assertGreater(later["project_commit_cut"], original["project_commit_cut"])
        current = self._read("read", ids=["strategy:strategy.theta.1"])
        self.assertNotEqual(current["result"]["items"][0]["id"], later["result"]["items"][0]["id"])

    def test_wrong_family_child_assignment_writer_and_inactive_epoch_are_rejected(self):
        self._start(("branches", "capture_artifacts"))
        for request in ({"mode": "record_strategy"}, {"operation": "record_strategy", "input": {}},
                        {"mode": "retrieve", "selection": {"mode": "proof_attention", "purpose": "not granted"}}):
            with self.subTest(request=request), self.assertRaises(MissionInterfaceError):
                self.interface.execute_research_read(request, grant=self.grant, binding=self.execution, research_query_context=self.key)
        for changes in ({"actual_child_thread_id": "child:sibling"}, {"actual_depth": 2},
                        {"expected_assignment_id": "another-assignment"}, {"expected_grant_id": "00" * 32}):
            with self.subTest(changes=changes), self.assertRaises(MissionInterfaceError):
                self.interface.execute_research_read({"mode": "usage"}, grant=self.grant,
                    binding={**self.execution, **changes}, research_query_context=self.key)
        with self.assertRaises(MissionInterfaceError):
            self._read("read", ids=["strategy:strategy.theta.1"])
        with self.assertRaises(MissionInterfaceError):
            self._read("read", ids=["capture-artifact:raw-capture:" + "a" * 48 + "#0"])
        historical = {**deep_thaw(self.grant), "schema_version": "mathematical_research.historical_read_grant.v1"}
        with self.assertRaises(MissionInterfaceError):
            self.interface.execute_research_read({"mode": "usage"}, grant=historical,
                binding=self.execution, research_query_context=self.key)
        with mock.patch.object(self.interface, "_direct_epoch_authority", return_value=None), self.assertRaises(MissionInterfaceError):
            self._read("history_inventory", source_families=["branches"])

    def test_selected_context_preserves_qualifications_and_rejects_missing_treatment(self):
        self._start(("contexts",))
        def treatment(account):
            return {"question": "Does this finite interface transfer?", "account": account,
                    "qualifications": ["Finite scope only."], "sources": []}
        document = {
            "schema_version": 3, "kind": "first_class_context", "project_id": "project.rh", "mission_id": "mission.1",
            "context_id": "research-selection", "purpose": "Qualified finite source.", "question": "What transfers?",
            "treatments": {"used": treatment("Exact used mathematics."), "unrelated": treatment("UNRELATED-BODY")},
            "exposed_treatments": [], "known_omissions": ["Uniform control is missing."], "restricted_uses": [],
            "restrictions": ["Assume n >= N."], "independence_treatment": {"treatment": "ordinary integrated judgment"},
        }
        result = self._execute(RECORD_CONTEXT, {"mode": "scientific", "updates": [{
            "context_id": "context:research-selection", "expected_head": None, "create": document, "patch": None,
        }], "exposure_required": []}, executive_epoch_id=self.epoch)
        self.assertEqual(result["status"], "completed", result)
        selected = self._read("selected_context", id="context:research-selection@1",
                              selection={"mode": "treatments", "treatment_ids": ["used"]})
        content = json.loads(selected["result"]["items"][0]["readable_content"])
        self.assertEqual(content["treatments"], {"used": document["treatments"]["used"]})
        self.assertEqual(content["context_qualifications"]["restrictions"], ["Assume n >= N."])
        self.assertNotIn("UNRELATED-BODY", json.dumps(content))
        with self.assertRaises(MissionInterfaceError):
            self._read("selected_context", id="context:research-selection@1",
                       selection={"mode": "treatments", "treatment_ids": ["absent"]})


if __name__ == "__main__":
    unittest.main()
