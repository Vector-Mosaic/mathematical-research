"""Default search-family coverage through real Mission Store owner boundaries.

No index return, source authenticator or projection is mocked. Expected handles
come from successful owner writes, and distinctive text lives in those owners.
These are provider-free structural fixtures, not research-behavior evidence.
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

PACKAGE_ROOT = Path(__file__).resolve().parents[1]
TEST_ROOT = Path(__file__).resolve().parent
for path in (PACKAGE_ROOT, TEST_ROOT):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

import test_mission_interface_direct as fixtures  # noqa: E402
from research_core.evidence_store import EvidenceCAS  # noqa: E402
from research_core.formal_session import (  # noqa: E402
    _current_formal_request, commit_formal_session_creation,
    prepare_formal_session_creation,
)
from research_core.mission_evidence import (  # noqa: E402
    CaptureScope, commit_capture_scope_annotation, commit_evidence_meaning,
    prepare_capture_scope_annotation, prepare_evidence_meaning,
)
from research_core.mission_interface import MissionInterface  # noqa: E402
from research_core.mission_operation_contract import (  # noqa: E402
    RECORD_BRANCH, RECORD_CANDIDATE, RECORD_CONTEXT, RECORD_STRATEGY, RETRIEVE,
    validate_semantic_result,
)
from research_core.research_model import deep_thaw  # noqa: E402


ROOT_FAMILIES = {
    "mission", "strategy", "branch", "candidate", "context", "evidence",
    "session", "capture", "capture-annotation",
}
DELEGATED_FAMILIES = (
    "branches", "candidates", "contexts", "strategies", "evidence",
    "capture_annotations", "captures", "capture_artifacts",
)


class OwnerContentRouteCoverageTests(unittest.TestCase):
    setUpClass = classmethod(fixtures.DirectMissionInterfaceTests.setUpClass.__func__)
    _request = staticmethod(fixtures.DirectMissionInterfaceTests._request)
    _authorize = fixtures.DirectMissionInterfaceTests._authorize
    _bind = fixtures.DirectMissionInterfaceTests._bind
    _execute = fixtures.DirectMissionInterfaceTests._execute
    _historical_execution_binding = staticmethod(
        fixtures.DirectMissionInterfaceTests._historical_execution_binding
    )

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.genesis_seed = fixtures._direct_genesis_fixture()
        # The common paging word already exists in the unchanged Mission proof
        # standard. Private authorization metadata is not root-search content.
        self.interface = MissionInterface.initialize_from_owner(
            Path(self.temporary.name) / "mission",
            project_id="project.rh", mission_id="mission.1",
            canonical_snapshot=self.canonical, genesis_seed=self.genesis_seed,
            owner_command_id="owner-content-route.bootstrap",
        )
        self.store = self.interface._store
        self.cas = EvidenceCAS(self.store.paths)
        self.goal_thread_id = "goal-thread:owner-content-route"
        self.goal_workspace = Path(self.temporary.name) / "goal"
        self.goal_workspace.mkdir()
        self.root_query_context = {"cursor_mac_key": "ab" * 32}
        self.epoch = self._authorize()
        self._bind(self.epoch)
        self.authority = self.interface._direct_epoch_authority(self.epoch)
        self.assertIsNotNone(self.authority)

        self.handles = {"mission": "mission:mission.1@1"}
        branch = self._write(RECORD_BRANCH, {
            "branch_id": "branch:branch.route",
            "question": "branchneedle: does the wider construction survive?",
            "leverage_fingerprint": "independent-wide-construction",
            "target_hook": "Preserve the qualified construction, not a proof claim.",
            # A later revision may refine residue, not replace Branch identity.
            "retained_residue": [{
                "residue": "independently qualified construction within the exact Mission scope.",
                "owner_refs": [{"id": "mission:mission.1"}],
            }],
        })
        self.handles["branch"] = self._handle(branch)
        candidate = self._write(RECORD_CANDIDATE, {
            "candidate_id": "candidate:candidate.route", "proposal_kind": "lemma",
            "exact_statement": "independently candidateneedle: the compact-domain lemma remains open.",
            "standing": {"status": "open", "basis": "A bounded unverified proposal."},
        })
        self.handles["candidate"] = self._handle(candidate)
        context = self._write(RECORD_CONTEXT, {
            # Session stores the exact Context reference, not copied bet text.
            # This identity also gives that real Session the common paging term.
            "context_id": "context:context.independently",
            "subject": "independently contextneedle: qualified construction for formal inspection.",
            "question": "Does this exact bounded construction satisfy its stated test?",
            "material": [{"id": "mission:mission.1", "why": "The exact Mission scope."}],
        })
        self.handles["context"] = self._handle(context)
        strategy = self._write(RECORD_STRATEGY, {
            "mission_continuation": "continue",
            "integrated_comparison": "independently strategyneedle: compare the qualified surviving construction.",
            "selected_bets": [{
                "bet": "independently formal inspection of the selected construction.",
                "discriminator": "The exact bounded construction passes or fails.",
                "formal_request": {
                    "purpose": "targeted_verification",
                    "context": {"id": context["record_id"]},
                },
            }],
            "reconsideration_conditions": [{"condition": "Reconsider on exact contrary evidence."}],
        })
        self.handles["strategy"] = self._handle(strategy)
        selection = _current_formal_request(
            self.store, authority=self.authority,
            selected_bet_sha256=str(strategy["formal_requests"][0]["selected_bet_sha256"]),
        )[2]
        prepared = prepare_formal_session_creation(
            self.store, authority=self.authority, formal_request=selection,
        )
        commit_formal_session_creation(
            self.store, authority=self.authority, prepared=prepared,
            lease=self.interface._writer_lease(), actor="owner-content-route-fixture",
        )
        self.session_id = str(prepared.record.document["session_id"])
        self.handles["session"] = f"session:{self.session_id}@1"

        capture = self.interface.capture_native_material_observation({
            "observationId": "independently-artifactneedle",
            "materialKind": "output", "content": "raw-body-only-never-indexed",
            "rootThreadId": self.goal_thread_id,
            "parentThreadId": self.goal_thread_id,
            "childThreadId": "child:independently-captureneedle",
        }, executive_epoch_id=self.epoch)
        self.handles["capture"] = str(capture["material_id"])
        capture_id = self.handles["capture"].removeprefix("capture:")
        self.artifact_handle = f"capture-artifact:{capture_id}#0"
        annotation = prepare_capture_scope_annotation(
            authority=self.authority, annotation_id="annotation.route",
            capture_id=capture_id, lifecycle="active",
            exact_scope={"artifact_ordinal": 0, "coverage": "complete artifact",
                         "description": "independently annotationneedle: reviewed neutral scope."},
        )
        commit_capture_scope_annotation(
            self.store, record=annotation, lease=self.interface._writer_lease(),
            actor="owner-content-route-fixture",
        )
        self.handles["capture-annotation"] = f"capture-annotation:{annotation.annotation_id}@1"
        evidence = prepare_evidence_meaning(
            authority=self.authority, evidence_id="evidence.route",
            statement="independently evidenceneedle: the bounded observation has a qualified interpretation.",
            exact_scope="The captured bounded observation only.", strength="heuristic",
            semantic_role="result", authority_basis="current executive interpretation",
            sources=(CaptureScope(capture_id=capture_id, artifact_ordinal=0,
                                  exact_scope={"coverage": "complete artifact"}),),
            non_inferences=("No general theorem follows.",),
            limitations=("Synthetic boundary fixture only.",),
        )
        commit_evidence_meaning(
            self.store, record=evidence, lease=self.interface._writer_lease(),
            actor="owner-content-route-fixture",
        )
        self.handles["evidence"] = f"evidence:{evidence.evidence.evidence_id}@1"

        complete = self._write(RECORD_CANDIDATE, {
            "candidate_id": "candidate:candidate.internal-intake",
            "proposal_kind": "proof_architecture",
            "exact_statement": "Every nontrivial zeta zero lies on the critical line.",
            "mechanism": "A purported unconditional argument, not verified mathematics.",
            "scope_and_reach": "complete proof of the Riemann Hypothesis",
            "complete_target_claim": {"target": "riemann_hypothesis", "disposition": "proof"},
            "standing": {"status": "open", "basis": "The purported proof needs adversarial review."},
            "non_inferences": ["No verified proof is claimed by this fixture."],
        })
        current = self.store.read_candidate_revision(
            mission_id="mission.1", candidate_id="candidate.internal-intake",
        )
        self.a1 = self.store.read_candidate_a1_binding(
            candidate_id="candidate.internal-intake", candidate_revision=int(complete["revision"]),
            candidate_digest=str(current["payload_digest"]),
        )
        self.a1.verify_issued()
        self._grant_all_delegated_families()

    @staticmethod
    def _handle(result):
        return f"{result['record_id']}@{result['revision']}"

    def _write(self, operation, value):
        response = self._execute(operation, value, executive_epoch_id=self.epoch)
        self.assertEqual(response["status"], "completed", response)
        return deep_thaw(response["result"])

    def _fresh(self):
        return MissionInterface.open(
            self.interface.workspace_root, expected_project_id="project.rh",
            expected_mission_id="mission.1",
        )

    def _root(self, query, *, service=None, **options):
        response = (service or self.interface).execute_semantic_operation(
            self._request(RETRIEVE, {"mode": "search", "purpose": "Exact route coverage.",
                                     "query": query, **options}),
            executive_epoch_id=self.epoch, root_query_context=self.root_query_context,
        )
        self.assertEqual(response["status"], "completed", response)
        validate_semantic_result(RETRIEVE, response)
        return deep_thaw(response["result"])

    def _delegated(self, query, *, service=None, **options):
        response = (service or self.interface).execute_delegated_read(
            self._request(RETRIEVE, {"mode": "history_search", "purpose": "Exact route coverage.",
                                     "query": query, "fields": ["title", "content"], **options}),
            grant=self.grant,
            binding=self._historical_execution_binding(self.binding, self.grant),
        )
        self.assertEqual(response["status"], "completed", response)
        return deep_thaw(response["result"])

    def _grant_all_delegated_families(self):
        assignment = "Compare retained qualified constructions across the authorized owner families."
        context = self._write(RECORD_CONTEXT, {
            "context_id": "context:context.advisory", "subject": "Historical route access assignment",
            "question": "Which retained exact material changes the selected scientific comparison?",
            "material": [{"id": "mission:mission.1", "why": "The Mission fixes scope only."}],
            "historical_advisory": {
                "assignment_mode": "historical_opportunity_scout", "search_lens": assignment,
                "source_families": list(DELEGATED_FAMILIES), "historical_references": [],
                "untrusted_material_locators": [], "known_omissions": [],
                "coverage_limits": ["The authenticated fixed-cut Mission history only."],
            },
        })
        self.binding = {
            "project_id": "project.rh", "mission_id": "mission.1",
            "executive_epoch_id": self.epoch, "root_thread_id": self.goal_thread_id,
            "actual_child_thread_id": "child:route-search", "actual_parent_thread_id": self.goal_thread_id,
            "actual_depth": 1,
        }
        self.grant = self.interface.issue_historical_read_grant({
            "child_thread_id": "child:route-search", "assignment_mode": "historical_opportunity_scout",
            "assignment": assignment, "context": {"id": context["record_id"], "revision": context["revision"]},
            "source_families": list(DELEGATED_FAMILIES), "raw_body_policy": "metadata_only",
        }, binding=self.binding)

    def test_defaults_find_one_exact_owner_in_every_root_and_delegated_family(self):
        needles = {
            "mission": "genuinely correct, novel, self-contained", "branch": "branchneedle",
            "candidate": "candidateneedle", "context": "contextneedle",
            "strategy": "strategyneedle", "session": self.session_id,
            "capture": "independently-captureneedle", "capture-annotation": "annotationneedle",
            "evidence": "evidenceneedle",
        }
        before = deep_thaw(self.store.read_metadata())
        for kind, query in needles.items():
            with self.subTest(surface="root", kind=kind):
                result = self._root(query)
                self.assertEqual([item["id"] for item in result["items"]], [self.handles[kind]])
                self.assertEqual(set(result["scope"]["selection"]["kinds"]), ROOT_FAMILIES)
                self.assertEqual(set(result["scope"]["selection"]["fields"]), {"id", "title", "content"})
                self.assertIsNone(result["next_cursor"])
        for kind in ROOT_FAMILIES - {"mission", "session"}:
            with self.subTest(surface="delegated", kind=kind):
                result = self._delegated(needles[kind])
                self.assertEqual([item["id"] for item in result["items"]], [self.handles[kind]])
                self.assertEqual(set(result["source_families"]), set(DELEGATED_FAMILIES))
        # The parent Capture legitimately repeats artifact metadata in content.
        # A title query uniquely selects the artifact without narrowing families.
        artifact = self._delegated("independently-artifactneedle", fields=["title"])
        self.assertEqual([item["id"] for item in artifact["items"]], [self.artifact_handle])
        self.assertEqual(set(artifact["source_families"]), set(DELEGATED_FAMILIES))
        self.assertEqual(self._root("raw-body-only-never-indexed")["items"], [])
        self.assertEqual(self._delegated("raw-body-only-never-indexed")["items"], [])
        self.assertEqual(deep_thaw(self.store.read_metadata()), before)

    def test_default_pages_hold_all_family_cut_across_real_later_owner_writes(self):
        expected_root = set(self.handles.values())
        expected_delegated = expected_root - {self.handles["mission"], self.handles["session"]}
        expected_delegated.add(self.artifact_handle)
        root = self._root("independently", page_size=1)
        delegated = self._delegated("independently", page_size=1)
        root_ids = [item["id"] for item in root["items"]]
        delegated_ids = [item["id"] for item in delegated["items"]]
        revised = self._write(RECORD_BRANCH, {
            "branch_id": "branch:branch.route",
            "question": "branchneedle: does the wider construction survive?",
            "leverage_fingerprint": "independent-wide-construction",
            "target_hook": "Preserve the qualified construction, not a proof claim.",
            "retained_residue": [{
                "residue": "A later qualified description outside the original query.",
                "owner_refs": [{"id": "mission:mission.1"}],
            }],
        })
        self.assertEqual(self._handle(revised), "branch:branch.route@2")
        later = self._write(RECORD_BRANCH, {
            "branch_id": "branch:branch.after-cut", "question": "independently later construction outside both cuts.",
            "leverage_fingerprint": "later-construction", "target_hook": "Do not leak across a fixed cut.",
        })
        root_cursor = root["next_cursor"]
        delegated_cursor = delegated["next_cursor"]
        while root_cursor is not None:
            root = self._root("independently", page_size=1, cursor=root_cursor, service=self._fresh())
            root_ids.extend(item["id"] for item in root["items"])
            self.assertNotEqual(root["next_cursor"], root_cursor)
            root_cursor = root["next_cursor"]
        while delegated_cursor is not None:
            delegated = self._delegated("independently", page_size=1, cursor=delegated_cursor, service=self._fresh())
            self.assertEqual(delegated["project_commit_cut"], self.grant["project_commit_cut"])
            delegated_ids.extend(item["id"] for item in delegated["items"])
            self.assertNotEqual(delegated["next_cursor"], delegated_cursor)
            delegated_cursor = delegated["next_cursor"]
        self.assertEqual(set(root_ids), expected_root)
        self.assertEqual(len(root_ids), len(set(root_ids)))
        self.assertEqual(set(delegated_ids), expected_delegated)
        self.assertEqual(len(delegated_ids), len(set(delegated_ids)))
        self.assertEqual(
            {item["id"] for item in self._root("independently")["items"]},
            expected_root - {self.handles["branch"]} | {self._handle(later)},
        )

    def test_internal_a1_evidence_is_retained_but_excluded_from_both_searches(self):
        with self.store.snapshot_connection() as connection:
            row = connection.execute(
                "SELECT evidence_id FROM evidence_item_revision WHERE evidence_id = ? AND revision = ?",
                (self.a1.evidence_id, self.a1.evidence_revision),
            ).fetchone()
        self.assertIsNotNone(row)
        before = deep_thaw(self.store.read_metadata())
        self.assertEqual(self._root(self.a1.evidence_id, fields=["id"])["items"], [])
        self.assertEqual(self._delegated(self.a1.evidence_id, fields=["id"])["items"], [])
        self.assertEqual(deep_thaw(self.store.read_metadata()), before)


if __name__ == "__main__":
    unittest.main()
