from __future__ import annotations

import base64
import errno
import hashlib
import json
import os
import sqlite3
import stat
import sys
import tempfile
import threading
import unittest
from collections.abc import Mapping
from contextlib import closing, contextmanager
from dataclasses import replace
from pathlib import Path
from unittest import mock


PACKAGE_ROOT = Path(__file__).resolve().parents[1]
TEST_ROOT = Path(__file__).resolve().parent
REPO_ROOT = Path(__file__).resolve().parents[3]
WORKSTATION_PACKAGES = REPO_ROOT / "packages"
for candidate in (PACKAGE_ROOT, TEST_ROOT, WORKSTATION_PACKAGES):
    if str(candidate) not in sys.path:
        sys.path.insert(0, str(candidate))

from research_attempt_adapter.testing import FakeAttemptProvider, FakeProviderBackend, FakeSourceVerifier

from research_attempt_adapter import (  # noqa: E402
    AttemptJournal,
    AttemptState,
    LaunchRequest,
    OutputArtifact,
    ProviderObservation,
    ProviderState,
    ResearchAttemptAdapter,
    sha256_file,
)
from research_core.canonical_admission import (  # noqa: E402
    prepare_admitted_research_state,
)
from research_core.canonical_snapshot import (  # noqa: E402
    CANONICAL_STATE_REPO_PATH,
    load_canonical_snapshot,
)
from research_core.candidate_revision import (  # noqa: E402
    _prepare_candidate_revision_v2_legacy_document,
    prepare_candidate_revision,
)
from research_core.complete_claim_admission import AdmissionEvidenceRef  # noqa: E402
from research_core.evidence_store import EvidenceCAS, EvidenceItemRevision  # noqa: E402
from research_core.formal_session import (  # noqa: E402
    _current_formal_request,
    commit_formal_session_creation,
    prepare_formal_session_creation,
    read_formal_session_revision,
)
from research_core.json_support import canonical_json_bytes  # noqa: E402
from research_core.mission_attempt_runtime import (  # noqa: E402
    FormalAttemptRuntimeFacts,
    execute_formal_attempt_operation,
)
from research_core.mission_evidence import (  # noqa: E402
    RawCaptureArtifactContent,
    RawCaptureArtifactDescriptor,
    RawCaptureArtifactFileInput,
    RawCaptureArtifactInput,
    commit_raw_capture,
    prepare_raw_capture,
    read_evidence_meaning,
    read_raw_capture,
    read_raw_capture_artifact,
)
from research_core.mission_interface import (  # noqa: E402
    CheckpointCommittedSourceHandoffError,
    MissionInterface,
    MissionInterfaceError,
)
from research_core.mission_executive import (  # noqa: E402
    OwnerRevisionRef,
    prepare_direct_executive_epoch_authorized_event,
)
from research_core.mission_frontier import (  # noqa: E402
    commit_strategy_revision,
    prepare_strategy_revision,
    read_mission_strategy_head,
)
from research_core.mission_owner import (  # noqa: E402
    SUCCESSOR_MISSION_OBJECTIVE,
    direct_mission_fence_successor,
    successor_mission_contract,
)
from research_core.observability import (  # noqa: E402
    CaptureWorkspaceObserver,
    EventKind,
)
from research_core.research_model import deep_thaw  # noqa: E402
from research_core.mission_operation_contract import (  # noqa: E402
    CHECKPOINT,
    INTERPRET_MATERIAL,
    MISSION_SEMANTIC_REQUEST_SCHEMA_VERSION,
    ORIENT,
    RECORD_BRANCH,
    RECORD_CANDIDATE,
    RECORD_CONTEXT,
    RECORD_STRATEGY,
    RETRIEVE,
    SYNTHESIZE,
)
from research_core.workspace_paths import (  # noqa: E402
    _issue_principal_attestation,
    attest_current_principal,
    stable_principal_owner_binding,
)
from research_core.workspace_schema import (  # noqa: E402
    IdentityKind,
    TypedWorkspaceId,
    canonical_payload,
    verify_schema_contract,
)
from research_core.workspace_store import (  # noqa: E402
    CheckpointSourceError,
    CheckpointSourceReacquisitionError,
    CommandConflictError,
    CommittedCommandAcknowledgementError,
    MissionFenceStaleCommandError,
    RevisionCommandFamily,
    RevisionWrite,
    StaleCommandError,
    StaleWriterError,
    WorkspaceIntegrityError,
    WorkspaceStore,
    WorkspaceStoreError,
)
from research_core_test_support import (  # noqa: E402
    independent_root6_body_at_commit,
    independent_root6_digest,
    independent_root6_member_mutations,
)


def _direct_genesis_fixture(
    *,
    project_id: str = "project.rh",
    mission_id: str = "mission.1",
    branch_id: str = "branch.theta",
    strategy_id: str = "strategy.theta.1",
) -> dict[str, object]:
    seed = json.loads(
        (
            REPO_ROOT
            / "contracts"
            / "rh_autonomous_mission_seed.v1.json"
        ).read_text(encoding="utf-8")
    )
    seed["project_id"] = project_id
    seed["authority"]["project_id"] = project_id
    mission = seed["mission"]
    mission["project_id"] = project_id
    mission["mission_id"] = mission_id
    mission["strategy_ids"] = [strategy_id]
    branch = seed["opening_branch"]
    branch["project_id"] = project_id
    branch["mission_id"] = mission_id
    branch["branch_id"] = branch_id
    strategy = seed["strategy"]
    strategy["project_id"] = project_id
    strategy["mission_id"] = mission_id
    strategy["strategy_id"] = strategy_id
    branch_digest = hashlib.sha256(canonical_json_bytes(branch)).hexdigest()

    def rebind_references(value: object) -> None:
        if isinstance(value, dict):
            if set(value) == {
                "kind",
                "identity",
                "revision",
                "payload_sha256",
            }:
                value.update(
                    {
                        "kind": "branch",
                        "identity": branch_id,
                        "revision": 1,
                        "payload_sha256": branch_digest,
                    }
                )
                return
            for child in value.values():
                rebind_references(child)
        elif isinstance(value, list):
            for child in value:
                rebind_references(child)

    rebind_references(strategy)
    return seed


class DirectMissionInterfaceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        loaded = load_canonical_snapshot(REPO_ROOT, source_commit="a" * 40)
        if not loaded.ok or loaded.value is None:
            raise AssertionError(loaded.failure)
        cls.canonical = loaded.value

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        workspace_root = Path(self.temporary.name) / "mission"
        self.genesis_seed = _direct_genesis_fixture()
        self.interface = MissionInterface.initialize_from_owner(
            workspace_root,
            project_id="project.rh",
            mission_id="mission.1",
            canonical_snapshot=self.canonical,
            genesis_seed=self.genesis_seed,
            owner_command_id="direct-mission-interface.bootstrap",
        )
        self.store = self.interface._store
        self.cas = EvidenceCAS(self.store.paths)
        self.goal_thread_id = "goal-thread:direct-mission-interface"
        self.goal_workspace = Path(self.temporary.name) / "goal-workspaces" / "direct"
        self.goal_workspace.mkdir(parents=True)

    @staticmethod
    def _request(operation: str, semantic_input: dict[str, object]) -> dict[str, object]:
        return {
            "schema_version": MISSION_SEMANTIC_REQUEST_SCHEMA_VERSION,
            "operation": operation,
            "input": semantic_input,
        }

    @staticmethod
    def _historical_execution_binding(
        binding: dict[str, object], grant: object
    ) -> dict[str, object]:
        if not isinstance(grant, Mapping):
            raise AssertionError("historical grant fixture must be a mapping")
        return {**binding, "expected_grant_id": grant["grant_id"]}

    @staticmethod
    def _linux_principal(*, uid: int = 1000):
        return _issue_principal_attestation(
            {
                "host_name": "research-fixture-host",
                "principal_name": "rh-runtime",
                "principal_sid": f"linux-uid:{uid}:effective-uid:{uid}",
                "session_id": 41,
                "process_id": os.getpid(),
            },
            platform_family="linux",
        )

    def _execute(
        self,
        operation: str,
        semantic_input: dict[str, object],
        *,
        executive_epoch_id: str,
    ) -> dict[str, object]:
        return dict(
            self.interface.execute_semantic_operation(
                self._request(operation, semantic_input),
                executive_epoch_id=executive_epoch_id,
            )
        )

    def _authorize(self) -> str:
        result = self.interface.authorize_executive_epoch_from_owner(
            {"expected_cut": self.interface.host_snapshot()["authorization_cut"]}
        )
        self.assertEqual(result["state"], "authorized")
        epoch_id = str(result["executive_epoch_id"])
        current = self.interface.current_epoch()
        self.assertEqual(current["state"], "authorized")
        self.assertEqual(current["entry"]["executive_epoch_id"], epoch_id)
        self.assertIsNone(current["entry"]["goal_thread_id"])
        return epoch_id

    def _bind(self, epoch_id: str) -> None:
        result = self.interface.bind_executive_epoch_from_owner(
            {
                "executiveEpochId": epoch_id,
                "rootThreadId": self.goal_thread_id,
                "workspaceRoot": str(self.goal_workspace.resolve()),
            }
        )
        self.assertEqual(result["state"], "bound")
        self.assertEqual(result["executive_epoch_id"], epoch_id)
        current = self.interface.current_epoch()
        self.assertEqual(current["state"], "open")
        self.assertEqual(current["entry"]["goal_thread_id"], self.goal_thread_id)
        self.assertEqual(
            current["entry"]["workspace_root"], str(self.goal_workspace.resolve())
        )

    def _assert_recovered_formal_session(
        self,
        *,
        session_id: str,
        expected_lifecycle: str,
        expected_revision: int,
    ) -> None:
        persisted = read_formal_session_revision(
            self.store,
            mission_id="mission.1",
            session_id=session_id,
        )
        self.assertEqual(persisted.revision, expected_revision)
        self.assertEqual(persisted.record.document["lifecycle"], expected_lifecycle)
        reconstruction = self.interface.reconstruct()
        session_deltas = [
            item
            for item in reconstruction["recovery_opening"]["owner_deltas"]
            if item["current_reference"]["kind"] == "session"
        ]
        self.assertEqual(len(session_deltas), 1)
        delta = session_deltas[0]
        self.assertEqual(delta["current_reference"], persisted.to_reference())
        self.assertEqual(delta["relation"], "no_prior_cut")
        self.assertEqual(
            delta["retrieval_handle"],
            f"session:{session_id}@{expected_revision}",
        )
        self.assertEqual(delta["summary"]["lifecycle"], expected_lifecycle)
        self.assertEqual(
            delta["summary"]["strategy_ref"],
            persisted.record.document["strategy_ref"],
        )
        self.assertEqual(
            delta["summary"]["selected_bet_sha256"],
            persisted.record.document["selected_bet_sha256"],
        )
        self.assertEqual(
            delta["summary"]["context_ref"],
            persisted.record.document["context_ref"],
        )
        self.assertEqual(
            delta["summary"]["terminal_binding"],
            persisted.record.document["terminal_binding"],
        )
        selected = self.interface.reconstruct(
            selected_readback_handles=[delta["retrieval_handle"]]
        )["selected_readback"]
        self.assertEqual(len(selected), 1)
        self.assertEqual(selected[0]["readback_kind"], "current_owner_document")
        self.assertEqual(selected[0]["current_reference"], persisted.to_reference())
        self.assertEqual(
            canonical_json_bytes(selected[0]["document"]),
            canonical_json_bytes(persisted.record.document),
        )

    def _assert_checkpoint_rejected(self, epoch_id: str) -> None:
        response = self.interface.execute_semantic_operation(
            self._request(CHECKPOINT, {}),
            executive_epoch_id=epoch_id,
        )
        self.assertEqual(response["status"], "rejected", response)
        self.assertIsNotNone(response["error"])
        self.assertIsNone(
            self.store.read_continuation_checkpoint(executive_epoch_id=epoch_id)
        )

    def _capture_pending_material(self, epoch_id: str) -> str:
        record = prepare_raw_capture(
            self.store,
            mission_id="mission.1",
            executive_epoch_id=epoch_id,
            capture_kind="output",
            observation_id="observation.pending-before-checkpoint",
            assignment_id="child-thread:pending-output",
            provenance={"kind": "direct_interface_test_fixture"},
            completion={"lifecycle": "completed"},
            artifacts=(
                RawCaptureArtifactInput(
                    role="result",
                    logical_name="pending-output.txt",
                    content_bytes=b"Pending factual output before checkpoint.",
                    media_type="text/plain",
                    encoding="utf-8",
                ),
            ),
        )
        commit_raw_capture(
            self.store,
            cas=self.cas,
            record=record,
            lease=self.store.reissue_writer_lease(attest_current_principal()),
            actor="direct-mission-interface-test",
        )
        return record.capture_id

    def _checkpoint_sections(
        self,
        checkpoint: Mapping[str, Any],
    ) -> Mapping[str, Any]:
        with self.store.direct_recovery_read_scope():
            facts = self.store.read_direct_recovery_facts(
                cut_project_commit=int(checkpoint["project_commit_no"])
            )
            return deep_thaw(
                self.store.materialize_continuation_checkpoint_sections(
                    checkpoint,
                    recovery_facts=facts,
                )
            )

    def test_assignment_orientation_retains_mission_ground_without_expanding_grant(self):
        epoch = self._authorize()
        self._bind(epoch)
        assignment = "Find retained mathematical uses of the Mission's parent question."
        context_id = "context:mission-purpose-only-advisory"
        result = self._execute(RECORD_CONTEXT, {
            "context_id": context_id, "subject": "Mission-purpose historical inquiry",
            "question": "Which retained construction could advance the parent question?",
            "material": [{"id": "mission:mission.1", "why": "Preserve the parent purpose and proof boundary."}],
            "historical_advisory": {
                "assignment_mode": "historical_opportunity_scout", "search_lens": assignment,
                "source_families": ["branches", "strategies"], "historical_references": [],
                "untrusted_material_locators": [], "known_omissions": [], "coverage_limits": [],
            },
        }, executive_epoch_id=epoch)
        self.assertEqual(result["status"], "completed", result)
        binding = {
            "project_id": "project.rh", "mission_id": "mission.1", "executive_epoch_id": epoch,
            "root_thread_id": self.goal_thread_id,
            "actual_child_thread_id": "child-thread:mission-purpose-only-advisory",
            "actual_parent_thread_id": self.goal_thread_id, "actual_depth": 1,
        }
        grant = self.interface.issue_historical_read_grant({
            "child_thread_id": binding["actual_child_thread_id"],
            "assignment_mode": "historical_opportunity_scout", "assignment": assignment,
            "context": {"id": context_id, "revision": result["result"]["revision"]},
            "source_families": ["branches", "strategies"], "raw_body_policy": "metadata_only",
        }, binding=binding)
        oriented = self.interface.execute_delegated_read(
            self._request(ORIENT, {}), grant=grant,
            binding=self._historical_execution_binding(binding, grant),
        )
        self.assertEqual(oriented["status"], "completed", oriented)
        orientation = oriented["result"]["orientation"]
        self.assertEqual(orientation["assignment_ground"], [])
        self.assertEqual(orientation["indispensable_ground"][0]["id"], "mission:mission.1")
        self.assertTrue(orientation["mission_purpose"])
        self.assertTrue(orientation["proof_boundary"])
        self.assertTrue(any("mission:mission.1@" in limit for limit in oriented["result"]["coverage"]["limits"]))

    def test_delegated_history_is_fixed_cut_scoped_and_cross_open_cursor_safe(
        self,
    ) -> None:
        epoch_id = self._authorize()
        self._bind(epoch_id)
        capture_id = self._capture_pending_material(epoch_id)

        candidate = self._execute(
            RECORD_CANDIDATE,
            {
                "candidate_id": "candidate:candidate.history-source",
                "proposal_kind": "lemma",
                "exact_statement": "A retained candidate supplies the exact bridge term.",
                "standing": {
                    "status": "open",
                    "basis": "The bridge remains to be tested.",
                },
            },
            executive_epoch_id=epoch_id,
        )
        self.assertEqual(candidate["status"], "completed", candidate)
        derived_candidate = self._execute(
            RECORD_CANDIDATE,
            {
                "candidate_id": "candidate:candidate.history-derived",
                "proposal_kind": "lemma",
                "exact_statement": "A derived candidate applies the retained bridge term.",
                "standing": {
                    "status": "open",
                    "basis": "The derived application remains to be tested.",
                },
                "genealogy": [
                    {
                        "relation": "derived_from",
                        "candidate": {
                            "id": "candidate:candidate.history-source"
                        },
                    }
                ],
            },
            executive_epoch_id=epoch_id,
        )
        self.assertEqual(derived_candidate["status"], "completed", derived_candidate)
        first_branch = self._execute(
            RECORD_BRANCH,
            {
                "branch_id": "branch:branch.history-lineage",
                "question": "Does the retained route expose the bridge?",
                "leverage_fingerprint": "history-lineage",
                "target_hook": "Test the retained candidate bridge.",
                "retained_residue": [
                    {
                        "residue": "the seeded lineage retains its original local term",
                        "owner_refs": [
                            {"id": "candidate:candidate.history-source"}
                        ],
                    }
                ],
                "owner_refs": [{"id": "candidate:candidate.history-source"}],
            },
            executive_epoch_id=epoch_id,
        )
        self.assertEqual(first_branch["status"], "completed", first_branch)
        self.assertEqual(first_branch["result"]["revision"], 1)
        second_branch = self._execute(
            RECORD_BRANCH,
            {
                "branch_id": "branch:branch.history-lineage",
                "question": "Does the retained route expose the bridge?",
                "leverage_fingerprint": "history-lineage",
                "target_hook": "Test the retained candidate bridge.",
                "retained_residue": [
                    {
                        "residue": "the revised route retains a different local term",
                        "owner_refs": [
                            {"id": "candidate:candidate.history-source"}
                        ],
                    }
                ],
                "owner_refs": [{"id": "candidate:candidate.history-source"}],
            },
            executive_epoch_id=epoch_id,
        )
        self.assertEqual(second_branch["status"], "completed", second_branch)
        self.assertEqual(second_branch["result"]["revision"], 2)
        unseeded_branch = self._execute(
            RECORD_BRANCH,
            {
                "branch_id": "branch:branch.history-overlooked",
                "question": "Can an unselected retained route expose a bridge?",
                "leverage_fingerprint": "history-overlooked",
                "target_hook": "Search independently of executive-selected history.",
                "retained_residue": [
                    {
                        "residue": "historical-first-route-phrase remains available",
                        "owner_refs": [
                            {"id": "candidate:candidate.history-source"}
                        ],
                    }
                ],
            },
            executive_epoch_id=epoch_id,
        )
        self.assertEqual(unseeded_branch["status"], "completed", unseeded_branch)
        self.assertEqual(unseeded_branch["result"]["revision"], 1)
        current_unseeded_branch = self._execute(
            RECORD_BRANCH,
            {
                "branch_id": "branch:branch.history-overlooked",
                "question": "Can an unselected retained route expose a bridge?",
                "leverage_fingerprint": "history-overlooked",
                "target_hook": "Search independently of executive-selected history.",
                "retained_residue": [
                    {
                        "residue": "The current route retains a replacement term.",
                        "owner_refs": [
                            {"id": "candidate:candidate.history-source"}
                        ],
                    }
                ],
            },
            executive_epoch_id=epoch_id,
        )
        self.assertEqual(
            current_unseeded_branch["status"],
            "completed",
            current_unseeded_branch,
        )
        self.assertEqual(current_unseeded_branch["result"]["revision"], 2)

        context_id = "context:context.history-advisory"
        recorded = self._execute(
            RECORD_CONTEXT,
            {
                "context_id": context_id,
                "subject": "Retained-history opportunity search",
                "question": "Can retained branch residue discharge the current bridge?",
                "material": [
                    {
                        "id": "branch:branch.history-lineage",
                        "why": "The current branch states the exact bottleneck.",
                    }
                ],
                "known_omissions": ["current external normalization literature"],
                "restricted_uses": ["Do not treat a retained analogy as a proof."],
                "restrictions": ["Use the current exact domain and normalization."],
                "dependencies": [
                    {
                        "id": "candidate:candidate.history-source",
                        "change_that_matters": "The bridge statement is revised.",
                        "dependent_judgment": "The historical search lens changes.",
                    }
                ],
                "historical_advisory": {
                    "assignment_mode": "historical_opportunity_scout",
                    "search_lens": "Find an overlooked retained bridge for the current branch.",
                    "source_families": [
                        "branches",
                        "candidates",
                        "capture_artifacts",
                    ],
                    "historical_references": [
                        {
                            "id": "branch:branch.history-lineage",
                            "revision": 1,
                            "why": "The original route may retain complementary residue.",
                        }
                    ],
                    "untrusted_material_locators": [
                        {
                            "id": f"capture-artifact:{capture_id}#0",
                            "why": "The exact raw observation may be inspected if authorized.",
                            "treatment": "untrusted_mathematical_material",
                        }
                    ],
                    "known_omissions": ["external literature"],
                    "coverage_limits": ["retained Mission material at the fixed cut"],
                },
            },
            executive_epoch_id=epoch_id,
        )
        self.assertEqual(recorded["status"], "completed", recorded)

        child_thread_id = "child-thread:history-scout"
        binding = {
            "project_id": "project.rh",
            "mission_id": "mission.1",
            "executive_epoch_id": epoch_id,
            "root_thread_id": self.goal_thread_id,
            "actual_child_thread_id": child_thread_id,
            "actual_parent_thread_id": self.goal_thread_id,
            "actual_depth": 1,
        }
        grant = self.interface.issue_historical_read_grant(
            {
                "child_thread_id": child_thread_id,
                "assignment_mode": "historical_opportunity_scout",
                "assignment": "Find an overlooked retained bridge for the current branch.",
                "context": {"id": context_id, "revision": 1},
                "source_families": [
                    "branches",
                    "candidates",
                    "capture_artifacts",
                ],
                "raw_body_policy": "metadata_only",
            },
            binding=binding,
        )
        cut = int(grant["project_commit_cut"])

        for field, forged_value in (
            ("raw_body_policy", "allow_untrusted_material"),
            ("project_commit_cut", 0),
        ):
            with self.subTest(rehashed_grant_field=field):
                rehashed_tamper = deep_thaw(grant)
                rehashed_tamper[field] = forged_value
                rehashed_tamper["grant_id"] = (
                    self.interface._historical_read_grant_id(
                        {
                            key: value
                            for key, value in rehashed_tamper.items()
                            if key != "grant_id"
                        }
                    )
                )
                with self.assertRaisesRegex(
                    MissionInterfaceError, "owner binding is invalid"
                ):
                    self.interface.execute_delegated_read(
                        self._request(ORIENT, {}),
                        grant=rehashed_tamper,
                        binding=self._historical_execution_binding(binding, grant),
                    )

        rehashed_scope_tamper = deep_thaw(grant)
        rehashed_scope_tamper["source_families"] = ["branches", "candidates"]
        rehashed_scope_tamper["grant_id"] = self.interface._historical_read_grant_id(
            {
                key: value
                for key, value in rehashed_scope_tamper.items()
                if key != "grant_id"
            }
        )
        with self.assertRaisesRegex(MissionInterfaceError, "persisted Context scope"):
            self.interface.execute_delegated_read(
                self._request(ORIENT, {}),
                grant=rehashed_scope_tamper,
                binding=self._historical_execution_binding(
                    binding, rehashed_scope_tamper
                ),
            )

        rehashed_assignment_tamper = deep_thaw(grant)
        rehashed_assignment_tamper["assignment_id"] = (
            "historical-assignment:" + "0" * 64
        )
        rehashed_assignment_tamper["grant_id"] = (
            self.interface._historical_read_grant_id(
                {
                    key: value
                    for key, value in rehashed_assignment_tamper.items()
                    if key != "grant_id"
                }
            )
        )
        with self.assertRaisesRegex(MissionInterfaceError, "assignment binding"):
            self.interface.execute_delegated_read(
                self._request(ORIENT, {}),
                grant=rehashed_assignment_tamper,
                binding=self._historical_execution_binding(
                    binding, rehashed_assignment_tamper
                ),
            )

        with mock.patch.object(self.store, "read_direct_recovery_facts",
                               side_effect=AssertionError("child orientation reconstructed all history")):
            oriented = self.interface.execute_delegated_read(
                self._request(ORIENT, {}),
                grant=grant,
                binding=self._historical_execution_binding(binding, grant),
            )
        self.assertEqual(oriented["status"], "completed", oriented)
        orientation = oriented["result"]["orientation"]
        self.assertEqual(orientation["context_id"], context_id)
        self.assertEqual(
            orientation["question"],
            "Can retained branch residue discharge the current bridge?",
        )
        self.assertEqual(oriented["result"]["items"], [])
        self.assertEqual(len(orientation["retrieval_routes"]), 4)
        self.assertEqual(
            orientation["context_purpose"],
            "Retained-history opportunity search",
        )
        self.assertEqual(
            orientation["indispensable_ground"],
            [
                {
                    "id": "branch:branch.history-lineage",
                    "revision": 2,
                    "why": "The current branch states the exact bottleneck.",
                }
            ],
        )
        self.assertEqual(
            orientation["known_omissions"],
            ["current external normalization literature"],
        )
        self.assertEqual(
            orientation["restricted_uses"],
            ["Do not treat a retained analogy as a proof."],
        )
        self.assertEqual(
            orientation["restrictions"],
            ["Use the current exact domain and normalization."],
        )
        self.assertEqual(
            orientation["invalidation_conditions"],
            [
                {
                    "id": "candidate:candidate.history-source",
                    "revision": 1,
                    "condition": (
                        "Change that matters: The bridge statement is revised. "
                        "Dependent judgment: The historical search lens changes."
                    ),
                }
            ],
        )
        self.assertEqual(orientation["schema_version"], "mathematical_research.historical_orientation.v2")
        self.assertTrue(orientation["mission_purpose"])
        self.assertTrue(orientation["proof_boundary"])
        ground_ids = {item["id"] for item in orientation["assignment_ground"]}
        self.assertNotIn("view:opportunity-portfolio", ground_ids)
        self.assertIn("branch:branch.history-lineage@2", ground_ids)
        self.assertNotIn("branch:branch.history-overlooked@2", ground_ids)
        self.assertNotIn("current_frontier", orientation)
        orientation_text = json.dumps(orientation, sort_keys=True)
        self.assertNotIn("immutable_historical_references", orientation_text)
        self.assertNotIn("branch:branch.history-lineage@1", orientation_text)
        self.assertNotIn("branch:branch.history-overlooked@1", orientation_text)
        self.assertNotIn("historical-first-route-phrase", orientation_text)
        self.assertNotIn(f"capture-artifact:{capture_id}#0", orientation_text)

        current_inventory = []
        current_cursor = None
        with mock.patch.object(self.interface, "_historical_item_from_record",
                               side_effect=AssertionError("inventory opened a body")):
            while True:
                inventory_input = {
                    "mode": "history_inventory", "purpose": "Deliberately inspect wider current branches.",
                    "source_families": ["branches"], "revision_scope": "current_at_cut", "page_size": 1,
                }
                if current_cursor is not None:
                    inventory_input["cursor"] = current_cursor
                current_page = self.interface.execute_delegated_read(
                    self._request(RETRIEVE, inventory_input), grant=grant,
                    binding=self._historical_execution_binding(binding, grant),
                )["result"]
                current_inventory.extend(current_page["items"])
                current_cursor = current_page["next_cursor"]
                if current_cursor is None:
                    break
        current_ids = {item["id"] for item in current_inventory}
        self.assertIn("branch:branch.history-overlooked@2", current_ids)
        self.assertIn("branch:branch.history-lineage@2", current_ids)
        self.assertNotIn("branch:branch.history-lineage@1", current_ids)
        self.assertTrue(all(item["completeness"] == "descriptor" for item in current_inventory))

        current_matches = self.interface.execute_delegated_read(
            self._request(RETRIEVE, {
                "mode": "history_search", "purpose": "Keep current search distinct from retained revisions.",
                "source_families": ["branches"], "revision_scope": "current_at_cut",
                "query": "branch.history", "fields": ["id"],
            }), grant=grant, binding=self._historical_execution_binding(binding, grant),
        )["result"]
        self.assertEqual({item["id"] for item in current_matches["items"]}, {
            "branch:branch.history-lineage@2", "branch:branch.history-overlooked@2",
        })

        searched = self.interface.execute_delegated_read(
            self._request(
                RETRIEVE,
                {
                    "mode": "history_search",
                    "purpose": "Find the unseeded first-route phrase.",
                    "query": "historical-first-route-phrase",
                    "fields": ["content"],
                },
            ),
            grant=grant,
            binding=self._historical_execution_binding(binding, grant),
        )
        self.assertEqual(
            [item["id"] for item in searched["result"]["items"]],
            ["branch:branch.history-overlooked@1"],
        )
        self.assertEqual(
            searched["result"]["items"][0]["match_provenance"],
            [{"field": "content", "query": "historical-first-route-phrase"}],
        )
        self.assertEqual(
            searched["result"]["items"][0]["trust_class"],
            "untrusted_mathematical_material",
        )

        history_search_base = {
            "mode": "history_search",
            "purpose": "Page every retained branch revision by exact identity text.",
            "source_families": ["branches"],
            "query": "branch.history",
            "fields": ["id"],
            "page_size": 1,
        }
        search_items = []
        search_cursor = None
        search_first_cursor = None
        while True:
            search_input = dict(history_search_base)
            if search_cursor is not None:
                search_input["cursor"] = search_cursor
            search_reader = (
                self.interface
                if search_cursor is None
                else MissionInterface.open(
                    self.interface.workspace_root,
                    expected_project_id="project.rh",
                    expected_mission_id="mission.1",
                )
            )
            search_page = search_reader.execute_delegated_read(
                self._request(RETRIEVE, search_input),
                grant=grant,
                binding=self._historical_execution_binding(binding, grant),
            )["result"]
            search_items.extend(search_page["items"])
            search_cursor = search_page["next_cursor"]
            if search_first_cursor is None:
                search_first_cursor = search_cursor
            if search_cursor is None:
                break
            self.assertEqual(len(search_page["items"]), 1)
        self.assertEqual(
            {item["id"] for item in search_items},
            {
                "branch:branch.history-lineage@1",
                "branch:branch.history-lineage@2",
                "branch:branch.history-overlooked@1",
                "branch:branch.history-overlooked@2",
            },
        )
        self.assertEqual(len(search_items), 4)
        self.assertIsNotNone(search_first_cursor)

        history_traverse_base = {
            "mode": "history_traverse",
            "purpose": "Follow exact retained lineage and owner references.",
            "ids": ["branch:branch.history-lineage@2"],
            "relationship_kinds": [
                "revision_predecessor",
                "owner_reference",
            ],
            "page_size": 1,
        }
        traversed_items = []
        traverse_cursor = None
        traverse_first_cursor = None
        while True:
            traverse_input = dict(history_traverse_base)
            if traverse_cursor is not None:
                traverse_input["cursor"] = traverse_cursor
            traverse_reader = (
                self.interface
                if traverse_cursor is None
                else MissionInterface.open(
                    self.interface.workspace_root,
                    expected_project_id="project.rh",
                    expected_mission_id="mission.1",
                )
            )
            with mock.patch.object(traverse_reader, "_historical_item_from_record",
                                   wraps=traverse_reader._historical_item_from_record) as body_reads:
                traverse_page = traverse_reader.execute_delegated_read(
                    self._request(RETRIEVE, traverse_input),
                    grant=grant,
                    binding=self._historical_execution_binding(binding, grant),
                )["result"]
            # One explicit source and at most one selected target, never every
            # outgoing target body before page selection.
            self.assertLessEqual(body_reads.call_count, 2)
            traversed_items.extend(traverse_page["items"])
            traverse_cursor = traverse_page["next_cursor"]
            if traverse_first_cursor is None:
                traverse_first_cursor = traverse_cursor
            if traverse_cursor is None:
                break
            self.assertEqual(len(traverse_page["items"]), 1)
        traversed_ids = {item["id"] for item in traversed_items}
        self.assertEqual(
            traversed_ids,
            {
                "branch:branch.history-lineage@1",
                "candidate:candidate.history-source@1",
            },
        )
        self.assertEqual(len(traversed_items), 2)
        self.assertIsNotNone(traverse_first_cursor)
        edge_kinds = {
            edge["relationship_kind"]
            for item in traversed_items
            for edge in item["edge_provenance"]
        }
        self.assertEqual(edge_kinds, {"revision_predecessor", "owner_reference"})

        alternate_binding = {
            **binding,
            "actual_child_thread_id": "child-thread:history-scout-alternate",
        }
        alternate_reader = MissionInterface.open(
            self.interface.workspace_root,
            expected_project_id="project.rh",
            expected_mission_id="mission.1",
        )
        alternate_grant = alternate_reader.issue_historical_read_grant(
            {
                "child_thread_id": alternate_binding["actual_child_thread_id"],
                "assignment_mode": "historical_opportunity_scout",
                "assignment": (
                    "Find an overlooked retained bridge for the current branch."
                ),
                "context": {"id": context_id, "revision": 1},
                "source_families": [
                    "branches",
                    "candidates",
                    "capture_artifacts",
                ],
                "raw_body_policy": "metadata_only",
            },
            binding=alternate_binding,
        )
        self.assertNotEqual(alternate_grant["grant_id"], grant["grant_id"])
        self.assertEqual(
            int(alternate_grant["project_commit_cut"]),
            int(grant["project_commit_cut"]),
        )
        alternate_execution_binding = self._historical_execution_binding(
            alternate_binding, alternate_grant
        )
        alternate_oriented = alternate_reader.execute_delegated_read(
            self._request(ORIENT, {}),
            grant=alternate_grant,
            binding=alternate_execution_binding,
        )
        self.assertEqual(alternate_oriented["status"], "completed")
        for base, grant_a_cursor in (
            (history_search_base, search_first_cursor),
            (history_traverse_base, traverse_first_cursor),
        ):
            with self.subTest(cursor_cross_grant_replay=base["mode"]):
                assert isinstance(grant_a_cursor, str)
                replay_input = dict(base)
                replay_input["cursor"] = grant_a_cursor
                with self.assertRaises(MissionInterfaceError) as raised:
                    alternate_reader.execute_delegated_read(
                        self._request(RETRIEVE, replay_input),
                        grant=alternate_grant,
                        binding=alternate_execution_binding,
                    )
                self.assertEqual(
                    raised.exception.code,
                    "historical_read_cursor_invalid",
                )
                self.assertIn("binding mismatch", str(raised.exception))

        before_cursor_tamper = int(
            self.store.read_metadata()["current_project_commit"]
        )
        for base, valid_cursor in (
            (history_search_base, search_first_cursor),
            (history_traverse_base, traverse_first_cursor),
        ):
            with self.subTest(cursor_mac_tamper=base["mode"]):
                assert isinstance(valid_cursor, str)
                sealed = bytearray(
                    base64.urlsafe_b64decode(
                        valid_cursor + "=" * (-len(valid_cursor) % 4)
                    )
                )
                sealed[-1] ^= 1
                tampered_cursor = base64.urlsafe_b64encode(sealed).decode(
                    "ascii"
                ).rstrip("=")
                tampered_input = dict(base)
                tampered_input["cursor"] = tampered_cursor
                with self.assertRaises(MissionInterfaceError) as raised:
                    self.interface.execute_delegated_read(
                        self._request(RETRIEVE, tampered_input),
                        grant=grant,
                        binding=self._historical_execution_binding(binding, grant),
                    )
                self.assertEqual(
                    raised.exception.code,
                    "historical_read_cursor_invalid",
                )
                self.assertIn("binding mismatch", str(raised.exception))
        self.assertEqual(
            int(self.store.read_metadata()["current_project_commit"]),
            before_cursor_tamper,
        )

        candidate_lineage = self.interface.execute_delegated_read(
            self._request(
                RETRIEVE,
                {
                    "mode": "history_traverse",
                    "purpose": "Follow the exact Candidate derived_from edge.",
                    "ids": ["candidate:candidate.history-derived@1"],
                    "relationship_kinds": ["candidate_genealogy"],
                },
            ),
            grant=grant,
            binding=self._historical_execution_binding(binding, grant),
        )
        self.assertEqual(
            [item["id"] for item in candidate_lineage["result"]["items"]],
            ["candidate:candidate.history-source@1"],
        )
        self.assertEqual(
            candidate_lineage["result"]["items"][0]["edge_provenance"],
            [
                {
                    "relationship_kind": "candidate_genealogy",
                    "source_id": "candidate:candidate.history-derived@1",
                    "target_id": "candidate:candidate.history-source@1",
                    "source_path": "/genealogy/0/candidate_ref",
                }
            ],
        )

        inventory_request = self._request(
            RETRIEVE,
            {
                "mode": "history_inventory",
                "purpose": "Page retained history.",
                "page_size": 1,
            },
        )
        first_page = self.interface.execute_delegated_read(
            inventory_request,
            grant=grant,
            binding=self._historical_execution_binding(binding, grant),
        )
        cursor = first_page["result"]["next_cursor"]
        self.assertIsInstance(cursor, str)
        self.assertIn(
            "next_cursor is null",
            first_page["result"]["coverage"]["meaning"],
        )
        reopened = MissionInterface.open(
            self.interface.workspace_root,
            expected_project_id="project.rh",
            expected_mission_id="mission.1",
        )
        second_page_request = self._request(
            RETRIEVE,
            {
                "mode": "history_inventory",
                "purpose": "Page retained history after a fresh open.",
                "page_size": 1,
                "cursor": cursor,
            },
        )
        second_page = reopened.execute_delegated_read(
            second_page_request,
            grant=grant,
            binding=self._historical_execution_binding(binding, grant),
        )
        self.assertNotEqual(
            first_page["result"]["items"][0]["id"],
            second_page["result"]["items"][0]["id"],
        )
        terminal_page = second_page["result"]
        continuation_count = 1
        while terminal_page["next_cursor"] is not None:
            continuation_count += 1
            self.assertLess(continuation_count, 20)
            terminal_page = reopened.execute_delegated_read(
                self._request(
                    RETRIEVE,
                    {
                        "mode": "history_inventory",
                        "purpose": "Exhaust the retained history page sequence.",
                        "page_size": 1,
                        "cursor": terminal_page["next_cursor"],
                    },
                ),
                grant=grant,
                binding=self._historical_execution_binding(binding, grant),
            )["result"]
        self.assertIsNone(terminal_page["next_cursor"])
        self.assertIn(
            "next_cursor is null",
            terminal_page["coverage"]["meaning"],
        )
        with self.assertRaisesRegex(MissionInterfaceError, "binding mismatch"):
            reopened.execute_delegated_read(
                self._request(
                    RETRIEVE,
                    {
                        "mode": "history_inventory",
                        "purpose": "Change the normalized query.",
                        "page_size": 2,
                        "cursor": cursor,
                    },
                ),
                grant=grant,
                binding=self._historical_execution_binding(binding, grant),
            )

        before_denials = int(self.store.read_metadata()["current_project_commit"])
        with self.assertRaisesRegex(MissionInterfaceError, "metadata only"):
            reopened.execute_delegated_read(
                self._request(
                    RETRIEVE,
                    {
                        "mode": "history_read",
                        "purpose": "Attempt a denied raw-body read.",
                        "ids": [f"capture-artifact:{capture_id}#0"],
                        "include_raw_bodies": True,
                    },
                ),
                grant=grant,
                binding=self._historical_execution_binding(binding, grant),
            )
        with self.assertRaises(MissionInterfaceError):
            reopened.execute_delegated_read(
                self._request(RECORD_BRANCH, {}),
                grant=grant,
                binding=self._historical_execution_binding(binding, grant),
            )
        self.assertEqual(
            int(self.store.read_metadata()["current_project_commit"]), before_denials
        )
        raw_grant = self.interface.issue_historical_read_grant(
            {
                "child_thread_id": child_thread_id,
                "assignment_mode": "historical_opportunity_scout",
                "assignment": "Find an overlooked retained bridge for the current branch.",
                "context": {"id": context_id, "revision": 1},
                "source_families": [
                    "branches",
                    "candidates",
                    "capture_artifacts",
                ],
                "raw_body_policy": "allow_untrusted_material",
            },
            binding=binding,
        )
        raw_read = reopened.execute_delegated_read(
            self._request(
                RETRIEVE,
                {
                    "mode": "history_read",
                    "purpose": "Read the explicitly authorized raw observation.",
                    "ids": [f"capture-artifact:{capture_id}#0"],
                    "include_raw_bodies": True,
                },
            ),
            grant=raw_grant,
            binding=self._historical_execution_binding(binding, raw_grant),
        )
        raw_item = raw_read["result"]["items"][0]
        self.assertEqual(raw_item["trust_class"], "untrusted_mathematical_material")
        self.assertEqual(raw_item["kind"], "capture-artifact")
        self.assertEqual(raw_item["title"], "pending-output.txt")
        self.assertEqual(
            json.loads(raw_item["readable_content"])["content"],
            "Pending factual output before checkpoint.",
        )

        postcut = self._execute(
            RECORD_BRANCH,
            {
                "branch_id": "branch:branch.postcut-only",
                "question": "Does postcut-only-needle exist?",
                "leverage_fingerprint": "postcut-only-fingerprint",
                "target_hook": "This branch must remain invisible to the old grant.",
            },
            executive_epoch_id=epoch_id,
        )
        self.assertEqual(postcut["status"], "completed", postcut)
        self.assertGreater(
            int(self.store.read_metadata()["current_project_commit"]), cut
        )
        invisible = reopened.execute_delegated_read(
            self._request(
                RETRIEVE,
                {
                    "mode": "history_search",
                    "purpose": "Check fixed-cut invisibility.",
                    "query": "postcut-only-needle",
                    "fields": ["content"],
                },
            ),
            grant=grant,
            binding=self._historical_execution_binding(binding, grant),
        )
        self.assertEqual(invisible["result"]["items"], [])

    def test_delegated_generic_evidence_discovery_and_exact_reads_share_grant_scope(self):
        epoch = self._authorize()
        self._bind(epoch)
        capture_id = self._capture_pending_material(epoch)
        evidence_id = "evidence.retained-continuity"

        def commit_revision(revision, statement):
            prior = (None if revision == 1 else
                     self.store.read_evidence_meaning_revision(evidence_id, revision - 1))
            record = EvidenceItemRevision(
                evidence_id=evidence_id, revision=revision, subtype="executive_continuity",
                subject={
                    "mission_id": "mission.1", "statement": statement,
                    "internal_note": "ctxh1/retained-private-reference",
                    "provenance": {"private": "withheld-provenance-marker"},
                },
                exact_scope="Only the retained bounded continuity judgment.",
                rigor="model_judgment", limitations=("Not a proved mathematical result.",),
                non_inferences=("Does not establish RH.",),
                security_classification="internal", retention="mission",
                blob_roles=(), availability_state="verified_available",
            )
            self.store.commit_evidence_meaning_revision(
                executive_epoch_id=epoch, record=record,
                sources=({"source_ordinal": 0, "capture_id": capture_id,
                          "artifact_ordinal": 0,
                          "exact_scope": {"scope": "The exact retained source."}},),
                expected_head_revision=None if prior is None else revision - 1,
                expected_head_payload_digest=None if prior is None else prior["payload_digest"],
                lease=self.store.reissue_writer_lease(attest_current_principal()),
                command_id=f"delegated.generic-evidence.{revision}", actor="test-fixture",
                expected_canonical_authority_digest=str(
                    self.store.read_metadata()["canonical_authority_digest"]),
            )

        commit_revision(1, "continuity-first-needle")
        commit_revision(2, "continuity-current-needle")
        assignment = "Compare the retained continuity judgments at this exact cut."
        context_id = "context:context.generic-evidence-history"
        context = self._execute(RECORD_CONTEXT, {
            "context_id": context_id, "subject": "Retained continuity",
            "question": "Which exact qualification survived the correction?",
            "material": [{"id": "branch:branch.theta", "why": "Keep the parent question."}],
            "historical_advisory": {
                "assignment_mode": "lifecycle_historian", "search_lens": assignment,
                "source_families": ["evidence"], "historical_references": [],
                "untrusted_material_locators": [], "known_omissions": [], "coverage_limits": [],
            },
        }, executive_epoch_id=epoch)
        self.assertEqual(context["status"], "completed", context)
        binding = {
            "project_id": "project.rh", "mission_id": "mission.1", "executive_epoch_id": epoch,
            "root_thread_id": self.goal_thread_id, "actual_parent_thread_id": self.goal_thread_id,
            "actual_child_thread_id": "child-thread:generic-evidence-history", "actual_depth": 1,
        }
        grant = self.interface.issue_historical_read_grant({
            "child_thread_id": binding["actual_child_thread_id"],
            "assignment_mode": "lifecycle_historian", "assignment": assignment,
            "context": {"id": context_id, "revision": context["result"]["revision"]},
            "source_families": ["evidence"], "raw_body_policy": "metadata_only",
        }, binding=binding)
        commit_revision(3, "continuity-future-needle")
        before_reads = int(self.store.read_metadata()["current_project_commit"])

        def retrieve(**selection):
            if selection.get("mode") == "history_read":
                # Exact historical reads require an explicit raw-body choice,
                # including denials that must reach the grant/cut boundary.
                selection.setdefault("include_raw_bodies", False)
            return self.interface.execute_delegated_read(
                self._request(RETRIEVE, {"purpose": assignment, **selection}), grant=grant,
                binding=self._historical_execution_binding(binding, grant),
            )

        for revision_scope, expected_revisions in (
            ("retained_history", [1, 2]), ("current_at_cut", [2]),
        ):
            for mode in ("history_inventory", "history_search"):
                with self.subTest(revision_scope=revision_scope, mode=mode):
                    options = ({"query": "continuity-", "fields": ["content"]}
                               if mode == "history_search" else {})
                    result = retrieve(mode=mode, revision_scope=revision_scope, **options)
                    self.assertEqual(result["status"], "completed", result)
                    self.assertIsNone(result["result"]["next_cursor"])
                    self.assertEqual([item["id"] for item in result["result"]["items"]], [
                        f"evidence:{evidence_id}@{revision}" for revision in expected_revisions
                    ])
        exact = retrieve(mode="history_read", ids=[f"evidence:{evidence_id}@1",
                                                     f"evidence:{evidence_id}"])
        self.assertEqual(exact["status"], "completed", exact)
        self.assertEqual([item["id"] for item in exact["result"]["items"]], [
            f"evidence:{evidence_id}@1", f"evidence:{evidence_id}@2",
        ])
        for item, statement in zip(exact["result"]["items"],
                                   ("continuity-first-needle", "continuity-current-needle")):
            document = json.loads(item["readable_content"])
            self.assertEqual(document["subtype"], "executive_continuity")
            self.assertEqual(document["subject"]["statement"], statement)
            self.assertEqual(document["exact_scope"], "Only the retained bounded continuity judgment.")
            self.assertEqual(document["limitations"], ["Not a proved mathematical result."])
            self.assertEqual(document["non_inferences"], ["Does not establish RH."])
            self.assertEqual(item["trust_class"], "untrusted_mathematical_material")
            for withheld in ("ctxh1", "withheld-provenance-marker", "Pending factual output before checkpoint."):
                self.assertNotIn(withheld, item["readable_content"])
        predecessor = retrieve(mode="history_traverse", ids=[f"evidence:{evidence_id}@2"],
                               relationship_kinds=["revision_predecessor"])
        self.assertEqual([item["id"] for item in predecessor["result"]["items"]],
                         [f"evidence:{evidence_id}@1"])
        for selector in (f"evidence:{evidence_id}@3", "branch:branch.theta@1"):
            with self.subTest(forbidden_selector=selector):
                with self.assertRaises(MissionInterfaceError) as denied:
                    retrieve(mode="history_read", ids=[selector])
                self.assertEqual(denied.exception.code, "historical_read_material_unavailable")
        with self.assertRaises(MissionInterfaceError) as denied:
            retrieve(mode="history_inventory", source_families=["branches"])
        self.assertEqual(denied.exception.code, "historical_read_scope_denied")
        with self.assertRaises(MissionInterfaceError) as denied:
            retrieve(mode="history_read", ids=[f"evidence:{evidence_id}@1"], include_raw_bodies=True)
        self.assertEqual(denied.exception.code, "historical_read_raw_body_denied")
        future = retrieve(mode="history_search", query="continuity-future-needle", fields=["content"])
        self.assertEqual(future["result"]["items"], [])

        # Cross-Mission rejection and owner integrity failures stay at the same
        # reader boundary; the fixture payload substitution is not custody proof.
        foreign = deep_thaw(self.store.read_evidence_meaning_revision(evidence_id, 1))
        foreign["record"]["subject"]["mission_id"] = "mission.other"
        with mock.patch.object(self.store, "read_evidence_meaning_revision", return_value=foreign):
            with self.assertRaises(MissionInterfaceError) as denied:
                retrieve(mode="history_read", ids=[f"evidence:{evidence_id}@1"])
            self.assertEqual(denied.exception.code, "historical_read_material_unavailable")
            with self.assertRaisesRegex(ValueError, "another Mission"):
                self.interface._historical_authoritative_document(f"evidence:{evidence_id}@1")
        with mock.patch.object(self.store, "read_evidence_meaning_revision",
                               side_effect=WorkspaceIntegrityError("authenticated Evidence is corrupt")):
            with self.assertRaises(WorkspaceIntegrityError):
                retrieve(mode="history_read", ids=[f"evidence:{evidence_id}@1"])
        self.assertEqual(int(self.store.read_metadata()["current_project_commit"]), before_reads)

    def test_lifecycle_historian_reconstructs_exact_strategy_decision_lineage(
        self,
    ) -> None:
        epoch_id = self._authorize()
        self._bind(epoch_id)
        generic_owner = self._execute(
            RECORD_CANDIDATE,
            {
                "candidate_id": "candidate:candidate.lifecycle-owner",
                "proposal_kind": "lemma",
                "exact_statement": "The generic decision input remains exact.",
                "standing": {"status": "open", "basis": "Retained for comparison."},
            },
            executive_epoch_id=epoch_id,
        )
        hook_owner = self._execute(
            RECORD_CANDIDATE,
            {
                "candidate_id": "candidate:candidate.lifecycle-revival",
                "proposal_kind": "lemma",
                "exact_statement": "A revival premise may reopen the prior route.",
                "standing": {"status": "open", "basis": "Retained as a hook."},
            },
            executive_epoch_id=epoch_id,
        )
        self.assertEqual(generic_owner["status"], "completed", generic_owner)
        self.assertEqual(hook_owner["status"], "completed", hook_owner)
        first = self._execute(
            RECORD_STRATEGY,
            {
                "mission_continuation": "continue",
                "integrated_comparison": (
                    "historian-first-decision retained the original route."
                ),
                "reconsideration_conditions": [
                    {"condition": "Reconsider if the original route loses its premise."}
                ],
                "revival_conditions": [
                    {
                        "condition": "Revive if the retained premise becomes usable.",
                        "owner_refs": [
                            {"id": "candidate:candidate.lifecycle-revival"}
                        ],
                    }
                ],
                "owner_refs": [{"id": "candidate:candidate.lifecycle-owner"}],
            },
            executive_epoch_id=epoch_id,
        )
        self.assertEqual(first["status"], "completed", first)
        second = self._execute(
            RECORD_STRATEGY,
            {
                "mission_continuation": "continue",
                "integrated_comparison": (
                    "historian-second-decision redirects attention after a new lemma."
                ),
                "reconsideration_conditions": [
                    {"condition": "Reconsider if the new lemma fails at exact scope."}
                ],
                "revival_conditions": [
                    {
                        "condition": "Revive if the retained premise becomes usable.",
                        "owner_refs": [
                            {"id": "candidate:candidate.lifecycle-revival"}
                        ],
                    }
                ],
                "owner_refs": [{"id": "candidate:candidate.lifecycle-owner"}],
            },
            executive_epoch_id=epoch_id,
        )
        self.assertEqual(second["status"], "completed", second)
        strategy_id = str(second["result"]["record_id"])
        first_revision = int(first["result"]["revision"])
        second_revision = int(second["result"]["revision"])
        self.assertEqual(second_revision, first_revision + 1)

        context_id = "context:context.lifecycle-history"
        context = self._execute(
            RECORD_CONTEXT,
            {
                "context_id": context_id,
                "subject": "A pending Strategy decision with retained lineage.",
                "question": "Did the new lemma justify redirecting Strategy attention?",
                "material": [
                    {
                        "id": strategy_id,
                        "why": "The current Strategy owns the pending decision.",
                    }
                ],
                "historical_advisory": {
                    "assignment_mode": "lifecycle_historian",
                    "search_lens": "Reconstruct the exact Strategy decision change.",
                    "source_families": ["candidates", "strategies"],
                    "historical_references": [
                        {
                            "id": strategy_id,
                            "revision": first_revision,
                            "why": "This is the exact prior decision revision.",
                        }
                    ],
                    "untrusted_material_locators": [],
                    "known_omissions": ["external literature and unretained deliberation"],
                    "coverage_limits": [
                        "Strategy revisions retained by this Mission at the fixed cut"
                    ],
                },
            },
            executive_epoch_id=epoch_id,
        )
        self.assertEqual(context["status"], "completed", context)

        child_thread_id = "child-thread:lifecycle-historian"
        binding = {
            "project_id": "project.rh",
            "mission_id": "mission.1",
            "executive_epoch_id": epoch_id,
            "root_thread_id": self.goal_thread_id,
            "actual_child_thread_id": child_thread_id,
            "actual_parent_thread_id": self.goal_thread_id,
            "actual_depth": 1,
        }
        assignment = "Reconstruct the exact Strategy decision change."
        grant = self.interface.issue_historical_read_grant(
            {
                "child_thread_id": child_thread_id,
                "assignment_mode": "lifecycle_historian",
                "assignment": assignment,
                "context": {"id": context_id, "revision": 1},
                "source_families": ["candidates", "strategies"],
                "raw_body_policy": "metadata_only",
            },
            binding=binding,
        )
        expected_cut = int(self.store.read_metadata()["current_project_commit"])
        self.assertEqual(grant["project_commit_cut"], expected_cut)

        oriented = self.interface.execute_delegated_read(
            self._request(ORIENT, {}),
            grant=grant,
            binding=self._historical_execution_binding(binding, grant),
        )["result"]
        self.assertEqual(oriented["project_commit_cut"], expected_cut)
        self.assertEqual(
            oriented["source_families"],
            ["candidates", "strategies"],
        )
        self.assertEqual(
            oriented["coverage"]["omissions"],
            ["external literature and unretained deliberation"],
        )
        self.assertEqual(
            oriented["coverage"]["limits"],
            ["Strategy revisions retained by this Mission at the fixed cut"],
        )
        self.assertEqual(
            oriented["orientation"]["assignment_mode"],
            "lifecycle_historian",
        )
        self.assertEqual(oriented["orientation"]["assignment"], assignment)

        searched = self.interface.execute_delegated_read(
            self._request(
                RETRIEVE,
                {
                    "mode": "history_search",
                    "purpose": "Find the exact prior decision wording.",
                    "query": "historian-first-decision",
                    "fields": ["content"],
                    "source_families": ["strategies"],
                    "page_size": 1,
                },
            ),
            grant=grant,
            binding=self._historical_execution_binding(binding, grant),
        )["result"]
        self.assertEqual(searched["project_commit_cut"], expected_cut)
        self.assertEqual(searched["source_families"], ["strategies"])
        self.assertEqual(
            searched["items"][0]["match_provenance"],
            [{"field": "content", "query": "historian-first-decision"}],
        )
        self.assertEqual(
            searched["items"][0]["id"],
            f"{strategy_id}@{first_revision}",
        )

        first_page = self.interface.execute_delegated_read(
            self._request(
                RETRIEVE,
                {
                    "mode": "history_inventory",
                    "purpose": "Page the retained Strategy decision lineage.",
                    "source_families": ["strategies"],
                    "page_size": 1,
                },
            ),
            grant=grant,
            binding=self._historical_execution_binding(binding, grant),
        )["result"]
        self.assertIsInstance(first_page["next_cursor"], str)
        second_page = self.interface.execute_delegated_read(
            self._request(
                RETRIEVE,
                {
                    "mode": "history_inventory",
                    "purpose": "Continue the retained Strategy lineage page.",
                    "source_families": ["strategies"],
                    "page_size": 1,
                    "cursor": first_page["next_cursor"],
                },
            ),
            grant=grant,
            binding=self._historical_execution_binding(binding, grant),
        )["result"]
        self.assertNotEqual(
            first_page["items"][0]["id"],
            second_page["items"][0]["id"],
        )
        self.assertEqual(second_page["project_commit_cut"], expected_cut)

        exact = self.interface.execute_delegated_read(
            self._request(
                RETRIEVE,
                {
                    "mode": "history_read",
                    "purpose": "Read both exact Strategy decision revisions.",
                    "ids": [
                        f"{strategy_id}@{first_revision}",
                        f"{strategy_id}@{second_revision}",
                    ],
                    "include_raw_bodies": False,
                },
            ),
            grant=grant,
            binding=self._historical_execution_binding(binding, grant),
        )["result"]
        exact_content = {
            item["id"]: json.loads(item["readable_content"])[
                "integrated_comparison"
            ]
            for item in exact["items"]
        }
        self.assertIn(
            "historian-first-decision",
            exact_content[f"{strategy_id}@{first_revision}"],
        )
        self.assertIn(
            "historian-second-decision",
            exact_content[f"{strategy_id}@{second_revision}"],
        )

        lineage = self.interface.execute_delegated_read(
            self._request(
                RETRIEVE,
                {
                    "mode": "history_traverse",
                    "purpose": "Follow the exact prior Strategy decision revision.",
                    "ids": [f"{strategy_id}@{second_revision}"],
                    "relationship_kinds": ["revision_predecessor"],
                },
            ),
            grant=grant,
            binding=self._historical_execution_binding(binding, grant),
        )["result"]
        self.assertEqual(
            [item["id"] for item in lineage["items"]],
            [f"{strategy_id}@{first_revision}"],
        )
        self.assertEqual(
            lineage["items"][0]["edge_provenance"][0]["source_path"],
            "/revision/predecessor",
        )
        classified_refs = self.interface.execute_delegated_read(
            self._request(
                RETRIEVE,
                {
                    "mode": "history_traverse",
                    "purpose": "Distinguish generic Strategy refs from lifecycle hooks.",
                    "ids": [f"{strategy_id}@{second_revision}"],
                    "relationship_kinds": ["owner_reference", "strategy_hook"],
                },
            ),
            grant=grant,
            binding=self._historical_execution_binding(binding, grant),
        )["result"]
        classified = {
            item["id"]: item["edge_provenance"] for item in classified_refs["items"]
        }
        self.assertEqual(
            classified["candidate:candidate.lifecycle-owner@1"],
            [
                {
                    "relationship_kind": "owner_reference",
                    "source_id": f"{strategy_id}@{second_revision}",
                    "target_id": "candidate:candidate.lifecycle-owner@1",
                    "source_path": "/owner_refs/0",
                }
            ],
        )
        self.assertEqual(
            classified["candidate:candidate.lifecycle-revival@1"],
            [
                {
                    "relationship_kind": "strategy_hook",
                    "source_id": f"{strategy_id}@{second_revision}",
                    "target_id": "candidate:candidate.lifecycle-revival@1",
                    "source_path": "/revival_conditions/0/owner_refs/0",
                }
            ],
        )

    def _assert_compact_recovery_projection(
        self, reconstruction: dict[str, object]
    ) -> None:
        paths: list[tuple[tuple[str, ...], str]] = []

        def visit(value: object, path: tuple[str, ...] = ()) -> None:
            if isinstance(value, dict):
                for key, child in value.items():
                    paths.append((path, key))
                    visit(child, (*path, key))
            elif isinstance(value, (list, tuple)):
                for index, child in enumerate(value):
                    visit(child, (*path, str(index)))

        visit(reconstruction)
        forbidden = {
            "payload",
            "events",
            "readable_content",
            "content",
            "content_bytes",
            "inline_bytes",
            "inline_utf8",
            "transcript",
            "disposition",
            "receipt",
            "score",
            "quota",
        }
        self.assertFalse(
            [(path, key) for path, key in paths if key in forbidden]
        )
        expected_document_paths = (
            []
            if reconstruction["recovery_opening"]["cut"] is None  # type: ignore[index]
            else [("recovery_opening", "cut")]
        )
        self.assertEqual(
            [path for path, key in paths if key == "document"],
            expected_document_paths,
        )

    def _prepare_exact_retrieval_fixture(
        self,
    ) -> tuple[str, bytes, bytes, object]:
        epoch_id = self._authorize()
        self._bind(epoch_id)
        exact_text = b'{"mission_id":"literal research text","value":"exact"}\n'
        exact_binary = b"\x00\xff\x10raw\x00bytes\xfe"
        record = prepare_raw_capture(
            self.store,
            mission_id="mission.1",
            executive_epoch_id=epoch_id,
            capture_kind="output",
            observation_id="observation.lossless-retrieval",
            assignment_id="child-thread:lossless-retrieval",
            provenance={"kind": "direct_interface_test_fixture"},
            completion={"lifecycle": "completed"},
            artifacts=(
                RawCaptureArtifactInput(
                    role="text_result",
                    logical_name="exact-result.jsonl",
                    content_bytes=exact_text,
                    media_type="application/x-ndjson",
                    encoding="utf-8",
                ),
                RawCaptureArtifactInput(
                    role="binary_result",
                    logical_name="exact-result.bin",
                    content_bytes=exact_binary,
                ),
            ),
        )
        commit_raw_capture(
            self.store,
            cas=self.cas,
            record=record,
            lease=self.store.reissue_writer_lease(attest_current_principal()),
            actor="direct-mission-interface-test",
        )
        self.assertEqual(
            self.store.list_mission_evidence_meaning_heads("mission.1"), ()
        )
        return epoch_id, exact_text, exact_binary, record

    def test_fresh_genesis_atomically_writes_exact_direct_revision_one_heads(
        self,
    ) -> None:
        metadata = self.store.read_metadata()
        self.assertEqual(metadata["current_project_commit"], 1)
        heads = {
            kind: self.store.get_head(TypedWorkspaceId(kind, identity))
            for kind, identity in (
                (IdentityKind.MISSION, "mission.1"),
                (IdentityKind.BRANCH, "branch.theta"),
                (IdentityKind.STRATEGY, "strategy.theta.1"),
            )
        }
        self.assertTrue(all(head is not None for head in heads.values()))
        self.assertEqual(
            {kind: head.reference.revision for kind, head in heads.items()},
            {
                IdentityKind.MISSION: 1,
                IdentityKind.BRANCH: 1,
                IdentityKind.STRATEGY: 1,
            },
        )
        mission = heads[IdentityKind.MISSION]
        assert mission is not None
        expected = json.loads(canonical_json_bytes(successor_mission_contract()))
        self.assertEqual(
            canonical_json_bytes(
                {
                    "purpose": mission.payload["purpose"],
                    "execution_policy": mission.payload["execution_policy"],
                }
            ),
            canonical_json_bytes(expected),
        )
        self.assertEqual(
            mission.payload["purpose"]["objective"],
            SUCCESSOR_MISSION_OBJECTIVE,
        )
        self.assertEqual(
            mission.payload["execution_policy"]["selected_capabilities"],
            {
                "local_roots": (),
                "mcp_servers": (),
                "apps": (),
                "browser": None,
            },
        )
        future = successor_mission_contract(
            selected_capabilities={
                "local_roots": (
                    {
                        "kind": "skill",
                        "id": "selected-math-skill",
                        "release_relative_path": "ai/skills/selected-math-skill",
                    },
                ),
                "mcp_servers": (),
                "apps": (),
                "browser": None,
            }
        )
        self.assertEqual(successor_mission_contract(future), future)
        with self.assertRaises(ValueError):
            successor_mission_contract(
                selected_capabilities={
                    "local_roots": (
                        {
                            "kind": "skill",
                            "id": "selected-math-skill",
                            "release_relative_path": "ai/skills/selected-math-skill",
                            "ambient_path": "forbidden",
                        },
                    ),
                    "mcp_servers": (),
                    "apps": (),
                    "browser": None,
                }
            )
        forbidden_field_fragments = {
            "token",
            "time",
            "worker",
            "tool",
            "payload",
            "context",
            "deadline",
        }

        def keys(value: object) -> list[str]:
            if isinstance(value, dict):
                return list(value) + [
                    nested
                    for child in value.values()
                    for nested in keys(child)
                ]
            if isinstance(value, (list, tuple)):
                return [nested for child in value for nested in keys(child)]
            return []

        self.assertFalse(
            [
                key
                for key in keys(expected)
                if any(fragment in key.casefold() for fragment in forbidden_field_fragments)
            ]
        )
        with self.store.snapshot_connection() as connection:
            self.assertEqual(
                connection.execute(
                    "SELECT COUNT(*) FROM mission_bundle_snapshot"
                ).fetchone()[0],
                0,
            )
            self.assertEqual(
                tuple(
                    row["command_kind"]
                    for row in connection.execute(
                        "SELECT command_kind FROM transition_journal "
                        "ORDER BY project_commit_no"
                    )
                ),
                ("initialize_direct_mission_genesis",),
            )
            self.assertEqual(
                connection.execute("SELECT COUNT(*) FROM writer_epoch").fetchone()[0],
                1,
            )

        replay = MissionInterface.initialize_from_owner(
            self.interface.workspace_root,
            project_id="project.rh",
            mission_id="mission.1",
            canonical_snapshot=self.canonical,
            genesis_seed=self.genesis_seed,
            owner_command_id="direct-mission-interface.bootstrap",
        )
        self.assertEqual(replay.workspace_root, self.interface.workspace_root)
        with self.store.snapshot_connection() as connection:
            self.assertEqual(
                connection.execute("SELECT COUNT(*) FROM writer_epoch").fetchone()[0],
                1,
            )
            self.assertEqual(
                connection.execute("SELECT COUNT(*) FROM command_result").fetchone()[0],
                1,
            )

        epoch = replay.authorize_executive_epoch_from_owner(
            {"expected_cut": replay.host_snapshot()["authorization_cut"]}
        )
        self.assertEqual(epoch["state"], "authorized")

    def test_fresh_genesis_rejects_malformed_or_cross_bound_direct_seed(self) -> None:
        for label, mutate in (
            (
                "obsolete Mission ceiling",
                lambda seed: seed["mission"].update(
                    {"unresolved_decision_bundle_ceiling": 16}
                ),
            ),
            (
                "extra field",
                lambda seed: seed.update({"aggregate": {}}),
            ),
            (
                "cross-bound Branch",
                lambda seed: seed["opening_branch"].update(
                    {"mission_id": "mission.other"}
                ),
            ),
            (
                "cross-bound authority",
                lambda seed: seed["authority"].update(
                    {"project_id": "project.other"}
                ),
            ),
        ):
            seed = json.loads(json.dumps(self.genesis_seed))
            mutate(seed)
            workspace = Path(self.temporary.name) / f"rejected-{label.replace(' ', '-')}"
            with self.subTest(label=label), self.assertRaises(MissionInterfaceError):
                MissionInterface.initialize_from_owner(
                    workspace,
                    project_id="project.rh",
                    mission_id="mission.1",
                    canonical_snapshot=self.canonical,
                    genesis_seed=seed,
                    owner_command_id=f"direct-mission-interface.rejected.{label}",
                )
            self.assertFalse(workspace.exists())

    def test_owner_initializer_replays_exactly_and_preserves_one_writer(self) -> None:
        root = Path(self.temporary.name) / "linux-state" / "missions" / "rh"
        principal = self._linux_principal()
        foreign_principal = self._linux_principal(uid=2000)
        command_id = "direct-owner-genesis.linux.1"
        with mock.patch(
            "research_core.mission_interface.attest_current_principal",
            return_value=principal,
        ):
            initialized = MissionInterface.initialize_from_owner(
                root,
                project_id="project.rh",
                mission_id="mission.1",
                canonical_snapshot=self.canonical,
                genesis_seed=self.genesis_seed,
                owner_command_id=command_id,
            )
            replay = MissionInterface.initialize_from_owner(
                root,
                project_id="project.rh",
                mission_id="mission.1",
                canonical_snapshot=self.canonical,
                genesis_seed=self.genesis_seed,
                owner_command_id=command_id,
            )
        self.assertEqual(replay.workspace_root, initialized.workspace_root)

        with initialized._store.snapshot_connection() as connection:
            writers = tuple(
                connection.execute(
                    "SELECT owner, creation_basis FROM writer_epoch ORDER BY epoch"
                )
            )
            results = tuple(
                connection.execute(
                    "SELECT command_id FROM command_result ORDER BY command_id"
                )
            )
        self.assertEqual(len(writers), 1)
        self.assertEqual(writers[0]["owner"], stable_principal_owner_binding(principal))
        self.assertEqual(
            writers[0]["creation_basis"],
            f"owner-authorized-rh-mission-genesis:{command_id}",
        )
        self.assertEqual(
            [row["command_id"] for row in results].count(command_id),
            1,
        )

        metadata = initialized._store.read_metadata()
        with self.assertRaises(StaleWriterError):
            initialized._store.claim_writer(
                owner=stable_principal_owner_binding(principal),
                creation_basis="concurrent-writer-must-fail",
                expected_project_commit=int(metadata["current_project_commit"]),
                expected_root_digest=str(metadata["current_root_digest"]),
                expected_canonical_authority_digest=str(
                    metadata["canonical_authority_digest"]
                ),
                expected_current_epoch=int(metadata["current_writer_epoch"]),
            )

        with mock.patch(
            "research_core.mission_interface.attest_current_principal",
            return_value=foreign_principal,
        ), self.assertRaises(StaleWriterError):
            initialized.authorize_executive_epoch_from_owner(
                {"expected_cut": initialized.host_snapshot()["authorization_cut"]}
            )

        with mock.patch(
            "research_core.mission_interface.attest_current_principal",
            return_value=principal,
        ):
            authorized = initialized.authorize_executive_epoch_from_owner(
                {"expected_cut": initialized.host_snapshot()["authorization_cut"]}
            )
        self.assertEqual(authorized["state"], "authorized")

        with mock.patch(
            "research_core.mission_interface.attest_current_principal",
            return_value=principal,
        ), self.assertRaises(MissionInterfaceError) as changed_command:
            MissionInterface.initialize_from_owner(
                root,
                project_id="project.rh",
                mission_id="mission.1",
                canonical_snapshot=self.canonical,
                genesis_seed=self.genesis_seed,
                owner_command_id="direct-owner-genesis.different",
            )
        self.assertEqual(
            changed_command.exception.code,
            "mission_interface_workspace_collision",
        )

    def test_owner_initializer_preserves_an_unrelated_root_collision(self) -> None:
        root = Path(self.temporary.name) / "owner-root-collision"
        root.mkdir()
        sentinel = root / "unrelated.txt"
        sentinel.write_text("preserve", encoding="utf-8")
        with self.assertRaises(MissionInterfaceError) as raised:
            MissionInterface.initialize_from_owner(
                root,
                project_id="project.rh",
                mission_id="mission.1",
                canonical_snapshot=self.canonical,
                genesis_seed=self.genesis_seed,
                owner_command_id="direct-owner-genesis.collision",
            )
        self.assertEqual(
            raised.exception.code,
            "mission_interface_workspace_collision",
        )
        self.assertEqual(sentinel.read_text(encoding="utf-8"), "preserve")

    def test_facade_does_not_advertise_or_retain_aggregate_operations(self) -> None:
        removed = (
            "capabilities",
            "expected_state",
            "mission_projection",
            "status",
            "read_evidence",
            "open_executive_epoch_from_owner",
            "record_failed_executive_epoch_from_owner",
        )
        self.assertFalse(
            [name for name in removed if hasattr(MissionInterface, name)]
        )

    def test_admission_decisions_survive_successor_epoch_roots(self) -> None:
        for split_decision_epoch in (False, True):
            for disposition in ("authorize_exact_delta", "reject"):
                with self.subTest(
                    split_decision_epoch=split_decision_epoch,
                    disposition=disposition,
                ):
                    suffix = f"{disposition}-{int(split_decision_epoch)}"
                    self.interface = MissionInterface.initialize_from_owner(
                        Path(self.temporary.name) / f"admission-{suffix}",
                        project_id="project.rh",
                        mission_id="mission.1",
                        canonical_snapshot=self.canonical,
                        genesis_seed=_direct_genesis_fixture(),
                        owner_command_id=f"admission.{suffix}.bootstrap",
                    )
                    self.store = self.interface._store
                    self.cas = EvidenceCAS(self.store.paths)

                    def bind_epoch(label: str) -> str:
                        self.goal_thread_id = f"root:{suffix}:{label}"
                        self.goal_workspace = (
                            Path(self.temporary.name) / f"goal-{suffix}-{label}"
                        )
                        self.goal_workspace.mkdir()
                        result = self._authorize()
                        self._bind(result)
                        return result

                    def child_binding(child: str) -> dict[str, object]:
                        return {
                            "project_id": "project.rh",
                            "mission_id": "mission.1",
                            "executive_epoch_id": epoch_id,
                            "root_thread_id": self.goal_thread_id,
                            "actual_parent_thread_id": self.goal_thread_id,
                            "actual_child_thread_id": child,
                            "actual_depth": 1,
                        }

                    epoch_id = bind_epoch("a")
                    author_root = self.goal_thread_id
                    candidate_id = f"candidate.admission-{suffix}"
                    complete = self._execute(
                        RECORD_CANDIDATE,
                        {
                            "candidate_id": f"candidate:{candidate_id}",
                            "proposal_kind": "mathematical_statement",
                            "exact_statement": "The synthetic exact argument proves RH.",
                            "complete_target_claim": {
                                "target": "riemann_hypothesis",
                                "disposition": "proof",
                            },
                            "standing": {
                                "status": "open",
                                "basis": "Synthetic complete-claim Admission fixture.",
                            },
                        },
                        executive_epoch_id=epoch_id,
                    )
                    self.assertEqual(complete["status"], "completed", complete)
                    candidate_ref = complete["result"]["open_candidate_a1"][
                        "candidate_ref"
                    ]
                    context = self._execute(
                        RECORD_CONTEXT,
                        {
                            "context_id": "context:context.admission-cross-epoch",
                            "subject": "One frozen complete-target claim",
                            "question": "Does the exact claim survive independent review?",
                            "material": [
                                {
                                    "id": f"candidate:{candidate_id}",
                                    "why": "The exact claim is the review subject.",
                                }
                            ],
                        },
                        executive_epoch_id=epoch_id,
                    )
                    self.assertEqual(context["status"], "completed", context)
                    context_ref = {
                        "id": "context:context.admission-cross-epoch",
                        "revision": 1,
                    }
                    selector = {
                        "id": f"candidate:{candidate_id}",
                        "revision": candidate_ref["revision"],
                        "payload_sha256": candidate_ref["payload_sha256"],
                    }
                    triage_thread = f"child:{suffix}:triage-a"
                    triage_binding = child_binding(triage_thread)
                    triage_grant = self.interface.issue_candidate_a1_review_grant(
                        {
                            "child_thread_id": triage_thread,
                            "assignment": "Independently triage the exact claim.",
                            "context": context_ref,
                            "candidate_ref": selector,
                        },
                        binding=triage_binding,
                    )
                    triage = self.interface.execute_candidate_a1_review(
                        {
                            "mode": "submit",
                            "disposition": "admission_ready",
                            "review_finding": "No material triage objection remains.",
                            "no_remaining_material_objection": True,
                        },
                        grant=triage_grant,
                        binding=triage_binding,
                    )
                    case = self.interface.open_complete_claim_admission_case(
                        {"candidate_ref": selector},
                        executive_epoch_id=epoch_id,
                    )
                    stale_thread = f"child:{suffix}:review-a"
                    stale_binding = child_binding(stale_thread)
                    stale_grant = self.interface.issue_admission_review_grant(
                        {
                            "child_thread_id": stale_thread,
                            "assignment": "This predecessor grant must expire at handoff.",
                            "context": context_ref,
                            "case_ref": case["case_ref"],
                        },
                        binding=stale_binding,
                    )
                    continued = self._execute(
                        RECORD_STRATEGY,
                        {
                            "mission_continuation": "continue",
                            "integrated_comparison": "Carry the exact A1 into successor review.",
                            "causal_inputs": [{
                                "source": {"id": f"candidate:{candidate_id}"},
                                "decision_consequence": "Preserve focused Admission review.",
                            }],
                            "reconsideration_conditions": [{
                                "condition": "React to the exact Admission outcome.",
                                "owner_refs": [{"id": f"candidate:{candidate_id}"}],
                            }],
                        },
                        executive_epoch_id=epoch_id,
                    )
                    self.assertEqual(continued["status"], "completed", continued)
                    checkpoint = self._execute(
                        CHECKPOINT, {}, executive_epoch_id=epoch_id
                    )
                    self.assertEqual(checkpoint["status"], "completed", checkpoint)
                    epoch_id = bind_epoch("b")
                    reviewer_root = self.goal_thread_id
                    grant_request = {
                        "assignment": "Independently review the frozen historical Case.",
                        "context": context_ref,
                        "case_ref": case["case_ref"],
                    }
                    before_denials = dict(self.store.read_metadata())
                    with self.assertRaisesRegex(StaleCommandError, "binding differs"):
                        self.interface.execute_admission_review(
                            {"mode": "usage"}, grant=stale_grant, binding=stale_binding
                        )
                    with self.assertRaises(MissionInterfaceError) as own_root:
                        self.interface.issue_admission_review_grant(
                            {**grant_request, "child_thread_id": self.goal_thread_id},
                            binding=child_binding(self.goal_thread_id),
                        )
                    self.assertEqual(
                        own_root.exception.code, "candidate_a1_review_binding_invalid"
                    )
                    for conflicting_thread in (author_root, triage_thread):
                        with self.assertRaises(MissionInterfaceError) as conflict:
                            self.interface.issue_admission_review_grant(
                                {
                                    **grant_request,
                                    "child_thread_id": conflicting_thread,
                                },
                                binding=child_binding(conflicting_thread),
                            )
                        self.assertEqual(
                            conflict.exception.code, "admission_role_conflict"
                        )
                    self.assertEqual(before_denials, dict(self.store.read_metadata()))
                    reviewer_thread = f"child:{suffix}:review-b"
                    reviewer_binding = child_binding(reviewer_thread)
                    review_grant = self.interface.issue_admission_review_grant(
                        {**grant_request, "child_thread_id": reviewer_thread},
                        binding=reviewer_binding,
                    )
                    cited_basis = [{
                        "kind": "candidate",
                        "identity": candidate_id,
                        "revision": candidate_ref["revision"],
                        "payload_sha256": candidate_ref["payload_sha256"],
                    }]
                    reviewed = self.interface.execute_admission_review(
                        {
                            "mode": "submit",
                            "disposition": "no_material_objection",
                            "review_finding": "The exact frozen claim survives review.",
                            "cited_basis": cited_basis,
                        },
                        grant=review_grant,
                        binding=reviewer_binding,
                    )
                    if split_decision_epoch:
                        checkpoint = self._execute(
                            CHECKPOINT, {}, executive_epoch_id=epoch_id
                        )
                        self.assertEqual(checkpoint["status"], "completed", checkpoint)
                        epoch_id = bind_epoch("c")
                    before_denials = dict(self.store.read_metadata())
                    if split_decision_epoch:
                        with self.assertRaisesRegex(StaleCommandError, "binding differs"):
                            self.interface.execute_admission_review(
                                {"mode": "usage"},
                                grant=review_grant,
                                binding=reviewer_binding,
                            )
                    for conflicting_thread in (
                        author_root, triage_thread, reviewer_thread
                    ):
                        with self.assertRaises(MissionInterfaceError) as conflict:
                            self.interface.issue_admission_decision_grant(
                                {
                                    **grant_request,
                                    "child_thread_id": conflicting_thread,
                                },
                                binding=child_binding(conflicting_thread),
                            )
                        self.assertEqual(
                            conflict.exception.code, "admission_role_conflict"
                        )
                    self.assertEqual(before_denials, dict(self.store.read_metadata()))
                    admitter_thread = f"child:{suffix}:admitter"
                    admitter_binding = child_binding(admitter_thread)
                    decision_grant = self.interface.issue_admission_decision_grant(
                        {**grant_request, "child_thread_id": admitter_thread},
                        binding=admitter_binding,
                    )
                    request = {
                        "mode": "submit",
                        "disposition": disposition,
                        "decision_basis": "The exact Case and Review ground this decision.",
                    }
                    if disposition == "reject":
                        request.update({
                            "objections": [{
                                "exact_objection": "The terminal implication omits one case.",
                                "affected_scope": "The frozen complete-target implication.",
                                "materiality_basis": "The omission defeats the complete claim.",
                            }],
                            "cited_basis": cited_basis,
                        })
                    canonical_before = self.store.read_metadata()[
                        "canonical_authority_digest"
                    ]
                    decided = self.interface.execute_admission_decision(
                        request, grant=decision_grant, binding=admitter_binding
                    )
                    self.assertEqual(decided["disposition"], disposition)
                    self.assertEqual(decided["canonical_effect"], "none")
                    self.assertEqual(decided["strategy_effect"], "none")
                    self.assertEqual(decided["public_effect"], "none")
                    reopened = MissionInterface.open(
                        self.interface.workspace_root,
                        expected_project_id="project.rh",
                        expected_mission_id="mission.1",
                    )
                    retained = next(
                        item for item in reopened._store.list_mission_candidate_a1_bindings(
                            "mission.1"
                        ) if item.candidate_id == candidate_id
                    )
                    self.assertEqual(retained.candidate_digest, selector["payload_sha256"])
                    self.assertEqual(retained.triage_disposition, "admission_ready")
                    self.assertEqual(
                        retained.triage_evidence_id,
                        triage["triage_evidence_ref"]["evidence_id"],
                    )
                    self.assertEqual(
                        retained.hold_lifecycle,
                        "resolved" if disposition == "reject" else "open",
                    )
                    self.assertIsNone(retained.admitted_result)
                    case_ref = AdmissionEvidenceRef(
                        case["case_ref"]["id"].removeprefix("evidence:"),
                        1,
                        case["case_ref"]["payload_sha256"],
                    )
                    review_ref = AdmissionEvidenceRef(
                        reviewed["record_ref"]["id"].removeprefix("evidence:"),
                        1,
                        reviewed["record_ref"]["payload_sha256"],
                    )
                    decision_ref = AdmissionEvidenceRef(
                        decided["record_ref"]["id"].removeprefix("evidence:"),
                        1,
                        decided["record_ref"]["payload_sha256"],
                    )
                    current_boundary = WorkspaceStore._verify_current_boundary
                    boundary_calls = 0

                    def verify_held_boundary(store, connection, *args, **kwargs):
                        nonlocal boundary_calls
                        self.assertTrue(connection.in_transaction)
                        boundary_calls += 1
                        return current_boundary(store, connection, *args, **kwargs)

                    with mock.patch.object(
                        WorkspaceStore,
                        "_verify_current_boundary",
                        new=verify_held_boundary,
                    ):
                        stored_case = (
                            reopened._store.read_complete_claim_admission_case(case_ref)
                        )
                        stored_review = (
                            reopened._store.read_complete_claim_admission_review(review_ref)
                        )
                        stored_decision = (
                            reopened._store.read_complete_claim_admission_decision(
                                decision_ref
                            )
                        )
                    self.assertEqual(boundary_calls, 3)
                    self.assertEqual(
                        stored_case.candidate_ref.digest_sha256,
                        selector["payload_sha256"],
                    )
                    self.assertEqual(stored_review.reviewer.root_thread_id, reviewer_root)
                    self.assertEqual(
                        stored_decision.admitter.root_thread_id, self.goal_thread_id
                    )
                    self.assertNotEqual(reviewer_root, author_root)
                    if split_decision_epoch:
                        self.assertNotEqual(
                            stored_decision.admitter.root_thread_id, reviewer_root
                        )
                    before_replay = dict(reopened._store.read_metadata())
                    replayed = reopened.execute_admission_decision(
                        request, grant=decision_grant, binding=admitter_binding
                    )
                    self.assertTrue(replayed["replayed"])
                    self.assertEqual(before_replay, dict(reopened._store.read_metadata()))
                    self.assertEqual(
                        reopened._store.read_metadata()["canonical_authority_digest"],
                        canonical_before,
                    )
                    with closing(
                        sqlite3.connect(reopened._store.paths.database)
                    ) as connection:
                        row = connection.execute(
                            "SELECT artifact.blob_sha256 "
                            "FROM evidence_capture_source AS source "
                            "JOIN raw_capture_artifact AS artifact "
                            "ON artifact.capture_id = source.capture_id "
                            "AND artifact.ordinal = source.artifact_ordinal "
                            "WHERE source.evidence_id = ? "
                            "AND source.evidence_revision = ?",
                            (review_ref.evidence_id, review_ref.revision),
                        ).fetchone()
                    self.assertIsNotNone(row)
                    assert row is not None
                    review_blob_path = EvidenceCAS(
                        reopened._store.paths
                    ).path_for_digest(str(row[0]))
                    review_blob_path.chmod(0o600)
                    review_blob_path.unlink()
                    before_unavailable = dict(reopened._store.read_metadata())
                    with self.assertRaisesRegex(
                        StaleCommandError,
                        "Blob bytes are unavailable or corrupt",
                    ):
                        reopened._store.read_complete_claim_admission_review(
                            review_ref
                        )
                    self.assertEqual(
                        before_unavailable,
                        dict(reopened._store.read_metadata()),
                    )

    def test_candidate_a1_review_grant_is_transitive_bounded_and_replayable(
        self,
    ) -> None:
        project_id = str(self.canonical.state["project"]["id"])
        self.genesis_seed = _direct_genesis_fixture(project_id=project_id)
        self.interface = MissionInterface.initialize_from_owner(
            Path(self.temporary.name) / "admission-mission",
            project_id=project_id,
            mission_id="mission.1",
            canonical_snapshot=self.canonical,
            genesis_seed=self.genesis_seed,
            owner_command_id="direct-mission-interface.admission-bootstrap",
        )
        self.store = self.interface._store
        self.cas = EvidenceCAS(self.store.paths)
        epoch_id = self._authorize()
        self._bind(epoch_id)
        basis_context = self._execute(
            RECORD_CONTEXT,
            {
                "context_id": "context:context.a1-basis",
                "subject": "Exact mathematical basis for one A1 fixture",
                "question": "Which exact normalization does the premise use?",
                "material": [
                    {
                        "id": "mission:mission.1",
                        "why": "The current Mission fixes the target and normalization.",
                    }
                ],
            },
            executive_epoch_id=epoch_id,
        )
        self.assertEqual(basis_context["status"], "completed", basis_context)
        premise = self._execute(
            RECORD_CANDIDATE,
            {
                "candidate_id": "candidate:candidate.a1-premise",
                "proposal_kind": "lemma",
                "exact_statement": "The normalized premise has the required sign.",
                "standing": {
                    "status": "open",
                    "basis": "The exact premise remains under review.",
                },
                "supporting_refs": [{"id": "context:context.a1-basis"}],
            },
            executive_epoch_id=epoch_id,
        )
        auxiliary = self._execute(
            RECORD_CANDIDATE,
            {
                "candidate_id": "candidate:candidate.a1-auxiliary",
                "proposal_kind": "lemma",
                "exact_statement": "The auxiliary estimate is compatible with the premise.",
                "standing": {
                    "status": "open",
                    "basis": "The synthesis determines only local compatibility.",
                },
            },
            executive_epoch_id=epoch_id,
        )
        unrelated = self._execute(
            RECORD_CANDIDATE,
            {
                "candidate_id": "candidate:candidate.a1-unrelated",
                "proposal_kind": "lemma",
                "exact_statement": "This workspace-near record is unrelated.",
                "standing": {
                    "status": "open",
                    "basis": "It is deliberately outside the A1 basis.",
                },
            },
            executive_epoch_id=epoch_id,
        )
        for response in (premise, auxiliary, unrelated):
            self.assertEqual(response["status"], "completed", response)
        relied_branch = self._execute(
            RECORD_BRANCH,
            {
                "branch_id": "branch:branch.a1-relied-basis",
                "question": "Which exact premise does this route actually rely on?",
                "leverage_fingerprint": "a1-relied-basis",
                "target_hook": "Retain only the explicit owner-ref premise.",
                "retained_residue": [
                    {
                        "residue": "An unrelated historical residue stays non-authoritative.",
                        "owner_refs": [
                            {"id": "candidate:candidate.a1-unrelated"}
                        ],
                    }
                ],
                "owner_refs": [{"id": "candidate:candidate.a1-premise"}],
            },
            executive_epoch_id=epoch_id,
        )
        self.assertEqual(relied_branch["status"], "completed", relied_branch)
        synthesis = self._execute(
            SYNTHESIZE,
            {
                "relation_question": "Do the premise and auxiliary estimate compose?",
                "inputs": [
                    {
                        "id": "candidate:candidate.a1-premise",
                        "role": "normalized premise",
                    },
                    {
                        "id": "candidate:candidate.a1-auxiliary",
                        "role": "auxiliary estimate",
                    },
                ],
                "compatibility_analysis": "The two exact meanings share one normalization.",
                "derivation_or_incompatibility": "They yield one local composition claim.",
                "scope": "The two exact Candidate revisions only.",
                "strength": "proof_outline",
                "edge_survival": "The local composition survives this exact check.",
                "consequences": [
                    {
                        "kind": "evidence",
                        "evidence_id": "evidence:evidence.a1-composition",
                        "statement": "The exact premise and auxiliary estimate compose locally.",
                        "scope": "The two exact Candidate inputs.",
                        "strength": "proof_outline",
                        "semantic_role": "composition",
                        "limitations": ["No complete RH conclusion follows by itself."],
                        "non_inferences": ["Do not infer RH from this local composition."],
                        "decision_consequence": "The complete Candidate may cite this basis.",
                    }
                ],
            },
            executive_epoch_id=epoch_id,
        )
        self.assertEqual(synthesis["status"], "completed", synthesis)
        complete = self._execute(
            RECORD_CANDIDATE,
            {
                "candidate_id": "candidate:candidate.a1-complete",
                "proposal_kind": "mathematical_statement",
                "exact_statement": "The cited exact composition completes a proof of RH.",
                "scope_and_reach": "complete unconditional proof of RH",
                "complete_target_claim": {
                    "target": "riemann_hypothesis",
                    "disposition": "proof",
                },
                "standing": {
                    "status": "open",
                    "basis": "Purported complete-target claim awaiting independent triage.",
                },
                "supporting_refs": [
                    {"id": "evidence:evidence.a1-composition"},
                    {"id": "branch:branch.a1-relied-basis"},
                ],
                "genealogy": [
                    {
                        "relation": "derived_from",
                        "candidate": {
                            "id": "candidate:candidate.a1-unrelated"
                        },
                    }
                ],
            },
            executive_epoch_id=epoch_id,
        )
        self.assertEqual(complete["status"], "completed", complete)
        a1_ref = complete["result"]["open_candidate_a1"]["candidate_ref"]
        review_context = self._execute(
            RECORD_CONTEXT,
            {
                "context_id": "context:context.a1-review",
                "subject": "Independent exact Candidate A1 review",
                "question": "Does the frozen complete claim survive reconstruction?",
                "material": [
                    {
                        "id": "candidate:candidate.a1-complete",
                        "why": "This is the exact frozen complete-target claim.",
                    }
                ],
            },
            executive_epoch_id=epoch_id,
        )
        self.assertEqual(review_context["status"], "completed", review_context)
        child_thread_id = "child-thread:candidate-a1-reviewer"
        owner_binding = {
            "project_id": project_id,
            "mission_id": "mission.1",
            "executive_epoch_id": epoch_id,
            "root_thread_id": self.goal_thread_id,
            "actual_child_thread_id": child_thread_id,
            "actual_parent_thread_id": self.goal_thread_id,
            "actual_depth": 1,
        }
        with (
            mock.patch.object(
                self.store,
                "list_mission_candidate_a1_bindings",
                side_effect=AssertionError("exact Candidate A1 grant enumerated history"),
            ),
            mock.patch(
                "research_core.workspace_store._validated_journal_index",
                side_effect=AssertionError("exact Candidate A1 grant built a full journal index"),
            ),
            mock.patch(
                "research_core.workspace_store._validated_transition_journal_chain",
                side_effect=AssertionError("exact Candidate A1 grant scanned the journal"),
            ),
        ):
            grant = self.interface.issue_candidate_a1_review_grant(
                {
                    "child_thread_id": child_thread_id,
                    "assignment": "Independently reconstruct the exact frozen A1.",
                    "context": {"id": "context:context.a1-review", "revision": 1},
                    "candidate_ref": {
                        "id": "candidate:candidate.a1-complete",
                        "revision": a1_ref["revision"],
                        "payload_sha256": a1_ref["payload_sha256"],
                    },
                },
                binding=owner_binding,
            )
        closure_ids = {
            f"{item['kind']}:{item['identity']}"
            for item in grant["source_closure"]
        }
        self.assertEqual(
            closure_ids,
            {
                "evidence:evidence.a1-composition",
                "candidate:candidate.a1-premise",
                "candidate:candidate.a1-auxiliary",
                "context:context.a1-basis",
                "branch:branch.a1-relied-basis",
            },
        )
        self.assertNotIn("candidate:candidate.a1-unrelated", closure_ids)

        usage = self.interface.execute_candidate_a1_review(
            {"mode": "usage"}, grant=grant, binding=owner_binding
        )
        self.assertEqual(usage["allowed_modes"], ("usage", "retrieve", "submit"))
        retrieved_ids: set[str] = set()
        cursor = None
        while True:
            retrieve_request = {"mode": "retrieve", "page_size": 2}
            if cursor is not None:
                retrieve_request["cursor"] = cursor
            retrieved = self.interface.execute_candidate_a1_review(
                retrieve_request,
                grant=grant,
                binding=owner_binding,
            )
            retrieved_ids.update(
                f"{item['reference']['kind']}:{item['reference']['identity']}"
                for item in retrieved["items"]
            )
            cursor = retrieved["next_cursor"]
            if cursor is None:
                break
        self.assertEqual(
            retrieved_ids,
            {
                "candidate:candidate.a1-complete",
                "context:context.a1-review",
                *closure_ids,
            },
        )

        submission = {
            "mode": "submit",
            "disposition": "admission_ready",
            "review_finding": (
                "Independent reconstruction found no remaining material objection."
            ),
            "no_remaining_material_objection": True,
            "limitations": ["This triage has no canonical or public effect."],
        }
        with (
            mock.patch.object(
                self.store,
                "list_mission_candidate_a1_bindings",
                side_effect=AssertionError("exact Candidate A1 submit enumerated history"),
            ),
            mock.patch(
                "research_core.workspace_store._validated_journal_index",
                side_effect=AssertionError("exact Candidate A1 submit built a full journal index"),
            ),
            mock.patch(
                "research_core.workspace_store._validated_transition_journal_chain",
                side_effect=AssertionError("exact Candidate A1 submit scanned the journal"),
            ),
        ):
            first = self.interface.execute_candidate_a1_review(
                submission,
                grant=grant,
                binding=owner_binding,
            )
        self.assertFalse(first["replayed"])
        triaged = next(
            item
            for item in self.store.list_mission_candidate_a1_bindings("mission.1")
            if item.candidate_id == "candidate.a1-complete"
        )
        self.assertEqual(triaged.triage_disposition, "admission_ready")
        self.assertEqual(triaged.hold_lifecycle, "open")
        reopened = MissionInterface.open(
            self.interface.workspace_root,
            expected_project_id=project_id,
            expected_mission_id="mission.1",
        )
        replay = reopened.execute_candidate_a1_review(
            submission,
            grant=grant,
            binding=owner_binding,
        )
        self.assertTrue(replay["replayed"])
        admission_ready_projection = next(
            item
            for item in reopened.reconstruct()["recovery_opening"][
                "open_candidate_a1"
            ]
            if item["candidate_ref"]["identity"] == "candidate.a1-complete"
        )
        self.assertEqual(
            admission_ready_projection["triage"]["disposition"],
            "admission_ready",
        )
        admission_ready_readbacks = reopened.reconstruct(
            selected_readback_handles=[
                admission_ready_projection["retrieval_handle"],
                admission_ready_projection["triage"]["retrieval_handle"],
            ]
        )["selected_readback"]
        self.assertEqual(
            admission_ready_readbacks[0]["candidate_a1"]["triage"],
            admission_ready_projection["triage"],
        )
        self.assertEqual(
            admission_ready_readbacks[1]["document"]["subject"]["disposition"],
            "admission_ready",
        )
        before_changed = int(self.store.read_metadata()["current_project_commit"])
        with self.assertRaises((StaleCommandError, ValueError)):
            self.interface.execute_candidate_a1_review(
                {
                    **submission,
                    "review_finding": "Changed replay must remain inert.",
                },
                grant=grant,
                binding=owner_binding,
            )
        self.assertEqual(
            int(self.store.read_metadata()["current_project_commit"]),
            before_changed,
        )

        with (
            mock.patch.object(
                self.store,
                "list_mission_candidate_a1_bindings",
                side_effect=AssertionError("exact Admission open enumerated A1 history"),
            ),
            mock.patch(
                "research_core.workspace_store._validated_journal_index",
                side_effect=AssertionError("exact Admission open built a full journal index"),
            ),
            mock.patch(
                "research_core.workspace_store._validated_transition_journal_chain",
                side_effect=AssertionError("exact Admission open scanned the journal"),
            ),
        ):
            admission_case = self.interface.open_complete_claim_admission_case(
                {
                    "candidate_ref": {
                        "id": "candidate:candidate.a1-complete",
                        "revision": a1_ref["revision"],
                        "payload_sha256": a1_ref["payload_sha256"],
                    }
                },
                executive_epoch_id=epoch_id,
            )
        self.assertEqual(admission_case["status"], "opened")
        self.assertEqual(admission_case["canonical_effect"], "none")
        replayed_case = self.interface.open_complete_claim_admission_case(
            {
                "candidate_ref": {
                    "id": "candidate:candidate.a1-complete",
                    "revision": a1_ref["revision"],
                    "payload_sha256": a1_ref["payload_sha256"],
                }
            },
            executive_epoch_id=epoch_id,
        )
        self.assertTrue(replayed_case["replayed"])
        self.assertEqual(replayed_case["case_ref"], admission_case["case_ref"])
        with self.assertRaises(StaleCommandError):
            self.interface.issue_admission_review_grant(
                {
                    "child_thread_id": "child-thread:stale-admission-reviewer",
                    "assignment": "This stale Case must remain inert.",
                    "context": {"id": "context:context.a1-review", "revision": 1},
                    "case_ref": {
                        **deep_thaw(admission_case["case_ref"]),
                        "payload_sha256": "0" * 64,
                    },
                },
                binding={
                    **owner_binding,
                    "actual_child_thread_id": "child-thread:stale-admission-reviewer",
                },
            )
        with self.assertRaises(MissionInterfaceError) as role_collision:
            self.interface.issue_admission_review_grant(
                {
                    "child_thread_id": child_thread_id,
                    "assignment": (
                        "The prior triage reviewer cannot independently review "
                        "the same Case."
                    ),
                    "context": {"id": "context:context.a1-review", "revision": 1},
                    "case_ref": admission_case["case_ref"],
                },
                binding=owner_binding,
            )
        self.assertEqual(role_collision.exception.code, "admission_role_conflict")
        admission_reviewer = "child-thread:complete-claim-admission-reviewer"
        admission_reviewer_binding = {
            **owner_binding,
            "actual_child_thread_id": admission_reviewer,
        }
        review_grant = self.interface.issue_admission_review_grant(
            {
                "child_thread_id": admission_reviewer,
                "assignment": "Independently review the exact frozen Admission Case.",
                "context": {"id": "context:context.a1-review", "revision": 1},
                "case_ref": admission_case["case_ref"],
            },
            binding=admission_reviewer_binding,
        )
        review_retrieval = self.interface.execute_admission_review(
            {"mode": "retrieve", "page_size": 50},
            grant=review_grant,
            binding=admission_reviewer_binding,
        )
        review_roles = {item["role"] for item in review_retrieval["items"]}
        self.assertIn("frozen_candidate_a1", review_roles)
        self.assertIn("admission_context", review_roles)
        self.assertIn("admission_case", review_roles)
        self.assertIn("candidate_authored_mathematical_basis", review_roles)
        case_item = next(
            item
            for item in review_retrieval["items"]
            if item["role"] == "admission_case"
        )
        self.assertEqual(
            case_item["document"]["subtype"],
            "complete_claim_admission_case",
        )
        self.assertIn(
            "canonical result creation remains a separate authorized effect",
            case_item["document"]["limitations"],
        )
        unrelated_owner = self.store.read_candidate_revision(
            mission_id="mission.1",
            candidate_id="candidate.a1-unrelated",
        )
        before_unrelated = int(self.store.read_metadata()["current_project_commit"])
        with self.assertRaises(MissionInterfaceError) as unrelated_basis:
            self.interface.execute_admission_review(
                {
                    "mode": "submit",
                    "disposition": "no_material_objection",
                    "review_finding": "This request cites material outside the Case.",
                    "cited_basis": [
                        {
                            "kind": "candidate",
                            "identity": "candidate.a1-unrelated",
                            "revision": unrelated_owner["revision"],
                            "payload_sha256": unrelated_owner["payload_digest"],
                        }
                    ],
                },
                grant=review_grant,
                binding=admission_reviewer_binding,
            )
        self.assertEqual(unrelated_basis.exception.code, "admission_basis_denied")
        self.assertEqual(
            int(self.store.read_metadata()["current_project_commit"]),
            before_unrelated,
        )
        review_result = self.interface.execute_admission_review(
            {
                "mode": "submit",
                "disposition": "no_material_objection",
                "review_finding": (
                    "Independent Admission reconstruction found no material objection."
                ),
                "cited_basis": [
                    {
                        "kind": "candidate",
                        "identity": "candidate.a1-complete",
                        "revision": a1_ref["revision"],
                        "payload_sha256": a1_ref["payload_sha256"],
                    },
                    next(
                        deep_thaw(item)
                        for item in review_grant["source_closure"]
                        if item["kind"] == "branch"
                    ),
                ],
                "limitations": ["Canonical mutation remains separate."],
            },
            grant=review_grant,
            binding=admission_reviewer_binding,
        )
        self.assertEqual(review_result["disposition"], "no_material_objection")
        self.assertIsNone(review_result["canonical_result_binding"])
        admitter = "child-thread:complete-claim-admitter"
        admitter_binding = {
            **owner_binding,
            "actual_child_thread_id": admitter,
        }
        decision_grant = self.interface.issue_admission_decision_grant(
            {
                "child_thread_id": admitter,
                "assignment": "Make the exact role-disjoint Admission decision.",
                "context": {"id": "context:context.a1-review", "revision": 1},
                "case_ref": admission_case["case_ref"],
            },
            binding=admitter_binding,
        )
        decision_retrieval = self.interface.execute_admission_decision(
            {"mode": "retrieve", "page_size": 50},
            grant=decision_grant,
            binding=admitter_binding,
        )
        review_item = next(
            item
            for item in decision_retrieval["items"]
            if item["role"] == "independent_admission_review"
        )
        self.assertEqual(
            review_item["document"]["subtype"],
            "complete_claim_admission_review",
        )
        self.assertIn(
            "Canonical mutation remains separate.",
            review_item["document"]["limitations"],
        )
        decision_roles = {item["role"] for item in decision_retrieval["items"]}
        self.assertIn("admission_context", decision_roles)
        decision_result = self.interface.execute_admission_decision(
            {
                "mode": "submit",
                "disposition": "authorize_exact_delta",
                "decision_basis": (
                    "The exact frozen proof and independent no-objection review qualify."
                ),
                "limitations": ["The external canonical writer has not run."],
            },
            grant=decision_grant,
            binding=admitter_binding,
        )
        self.assertEqual(decision_result["disposition"], "authorize_exact_delta")
        self.assertEqual(decision_result["canonical_result_binding"]["result"], "proved")
        self.assertEqual(decision_result["canonical_effect"], "none")
        retained_a1 = next(
            item
            for item in self.store.list_mission_candidate_a1_bindings("mission.1")
            if item.candidate_id == "candidate.a1-complete"
        )
        self.assertEqual(retained_a1.hold_lifecycle, "open")
        self.assertEqual(retained_a1.triage_disposition, "admission_ready")
        reopened_decision = MissionInterface.open(
            self.interface.workspace_root,
            expected_project_id=project_id,
            expected_mission_id="mission.1",
        )._store.read_complete_claim_admission_decision(
            AdmissionEvidenceRef(
                decision_result["record_ref"]["id"].removeprefix("evidence:"),
                decision_result["record_ref"]["revision"],
                decision_result["record_ref"]["payload_sha256"],
            )
        )
        self.assertEqual(reopened_decision.disposition, "authorize_exact_delta")
        replayed_decision = reopened.execute_admission_decision(
            {
                "mode": "submit",
                "disposition": "authorize_exact_delta",
                "decision_basis": (
                    "The exact frozen proof and independent no-objection review qualify."
                ),
                "limitations": ["The external canonical writer has not run."],
            },
            grant=decision_grant,
            binding=admitter_binding,
        )
        self.assertTrue(replayed_decision["replayed"])

        invalid_candidate = self._execute(
            RECORD_CANDIDATE,
            {
                "candidate_id": "candidate:candidate.a1-invalidated",
                "proposal_kind": "mathematical_statement",
                "exact_statement": "A second exact composition purports to prove RH.",
                "scope_and_reach": "complete unconditional proof of RH",
                "complete_target_claim": {
                    "target": "riemann_hypothesis",
                    "disposition": "proof",
                },
                "standing": {
                    "status": "open",
                    "basis": "Purported complete-target claim awaiting review.",
                },
                "supporting_refs": [{"id": "evidence:evidence.a1-composition"}],
            },
            executive_epoch_id=epoch_id,
        )
        self.assertEqual(invalid_candidate["status"], "completed", invalid_candidate)
        invalid_ref = invalid_candidate["result"]["open_candidate_a1"][
            "candidate_ref"
        ]
        parallel_a1 = next(
            item
            for item in self.store.list_mission_candidate_a1_bindings("mission.1")
            if item.candidate_id == "candidate.a1-invalidated"
        )
        self.assertEqual(parallel_a1.hold_lifecycle, "open")
        self.assertIsNone(parallel_a1.triage_disposition)
        invalid_context = self._execute(
            RECORD_CONTEXT,
            {
                "context_id": "context:context.a1-invalidation-review",
                "subject": "Independent review of the second frozen A1",
                "question": "Does one newly discovered defect defeat the claim?",
                "material": [
                    {
                        "id": "candidate:candidate.a1-invalidated",
                        "why": "This is the exact Candidate under review.",
                    }
                ],
            },
            executive_epoch_id=epoch_id,
        )
        self.assertEqual(invalid_context["status"], "completed", invalid_context)
        invalid_child = "child-thread:candidate-a1-invalidator"
        invalid_binding = {
            **owner_binding,
            "actual_child_thread_id": invalid_child,
        }
        invalid_grant = self.interface.issue_candidate_a1_review_grant(
            {
                "child_thread_id": invalid_child,
                "assignment": "Independently test the second frozen A1.",
                "context": {
                    "id": "context:context.a1-invalidation-review",
                    "revision": 1,
                },
                "candidate_ref": {
                    "id": "candidate:candidate.a1-invalidated",
                    "revision": invalid_ref["revision"],
                    "payload_sha256": invalid_ref["payload_sha256"],
                },
            },
            binding=invalid_binding,
        )
        invalid_triage = self.interface.execute_candidate_a1_review(
            {
                "mode": "submit",
                "disposition": "admission_ready",
                "review_finding": (
                    "Independent triage found a purported complete claim requiring "
                    "the separate Admission plane."
                ),
                "no_remaining_material_objection": True,
            },
            grant=invalid_grant,
            binding=invalid_binding,
        )
        self.assertEqual(invalid_triage["disposition"], "admission_ready")
        invalid_binding_state = next(
            item
            for item in self.store.list_mission_candidate_a1_bindings("mission.1")
            if item.candidate_id == "candidate.a1-invalidated"
        )
        self.assertEqual(invalid_binding_state.triage_disposition, "admission_ready")
        self.assertEqual(invalid_binding_state.hold_lifecycle, "open")

        rejected_case = self.interface.open_complete_claim_admission_case(
            {
                "candidate_ref": {
                    "id": "candidate:candidate.a1-invalidated",
                    "revision": invalid_ref["revision"],
                    "payload_sha256": invalid_ref["payload_sha256"],
                }
            },
            executive_epoch_id=epoch_id,
        )
        rejected_reviewer = "child-thread:complete-claim-rejection-reviewer"
        rejected_reviewer_binding = {
            **owner_binding,
            "actual_child_thread_id": rejected_reviewer,
        }
        rejected_review_grant = self.interface.issue_admission_review_grant(
            {
                "child_thread_id": rejected_reviewer,
                "assignment": "Independently identify any fatal exact objection.",
                "context": {
                    "id": "context:context.a1-invalidation-review",
                    "revision": 1,
                },
                "case_ref": rejected_case["case_ref"],
            },
            binding=rejected_reviewer_binding,
        )
        exact_objection = {
            "exact_objection": "The implication omits the required boundary case.",
            "affected_scope": "The claimed unconditional final implication.",
            "materiality_basis": (
                "Without that boundary case, the complete RH claim does not follow."
            ),
        }
        rejected_review = self.interface.execute_admission_review(
            {
                "mode": "submit",
                "disposition": "no_material_objection",
                "review_finding": (
                    "The independent reviewer found no material objection; the later "
                    "admitter must still be able to preserve a newly discovered fatal defect."
                ),
                "cited_basis": [
                    {
                        "kind": "candidate",
                        "identity": "candidate.a1-invalidated",
                        "revision": invalid_ref["revision"],
                        "payload_sha256": invalid_ref["payload_sha256"],
                    }
                ],
            },
            grant=rejected_review_grant,
            binding=rejected_reviewer_binding,
        )
        self.assertEqual(rejected_review["disposition"], "no_material_objection")
        rejected_admitter = "child-thread:complete-claim-rejection-admitter"
        rejected_admitter_binding = {
            **owner_binding,
            "actual_child_thread_id": rejected_admitter,
        }
        rejected_decision_grant = self.interface.issue_admission_decision_grant(
            {
                "child_thread_id": rejected_admitter,
                "assignment": "Decide the exact frozen Case from the fatal objection.",
                "context": {
                    "id": "context:context.a1-invalidation-review",
                    "revision": 1,
                },
                "case_ref": rejected_case["case_ref"],
            },
            binding=rejected_admitter_binding,
        )
        before_denied_reject = int(
            self.store.read_metadata()["current_project_commit"]
        )
        canonical_before_reject = deep_thaw(self.canonical.state)
        with self.assertRaises(MissionInterfaceError) as bare_reject:
            self.interface.execute_admission_decision(
                {
                    "mode": "submit",
                    "disposition": "reject",
                    "decision_basis": "Operational caution is not a fatal objection.",
                },
                grant=rejected_decision_grant,
                binding=rejected_admitter_binding,
            )
        self.assertEqual(
            bare_reject.exception.code,
            "admission_decision_request_invalid",
        )
        with self.assertRaises(MissionInterfaceError) as outside_reject_basis:
            self.interface.execute_admission_decision(
                {
                    "mode": "submit",
                    "disposition": "reject",
                    "decision_basis": "An unrelated Candidate cannot defeat this Case.",
                    "objections": [exact_objection],
                    "cited_basis": [
                        {
                            "kind": "candidate",
                            "identity": "candidate.a1-unrelated",
                            "revision": unrelated_owner["revision"],
                            "payload_sha256": unrelated_owner["payload_digest"],
                        }
                    ],
                },
                grant=rejected_decision_grant,
                binding=rejected_admitter_binding,
            )
        self.assertEqual(outside_reject_basis.exception.code, "admission_basis_denied")
        self.assertEqual(
            int(self.store.read_metadata()["current_project_commit"]),
            before_denied_reject,
        )
        rejected_decision_request = {
            "mode": "submit",
            "disposition": "reject",
            "decision_basis": (
                "The exact cited omission is sufficient to defeat this frozen claim."
            ),
            "objections": [exact_objection],
            "cited_basis": [
                {
                    "kind": "candidate",
                    "identity": "candidate.a1-invalidated",
                    "revision": invalid_ref["revision"],
                    "payload_sha256": invalid_ref["payload_sha256"],
                }
            ],
        }
        invalidated = self.interface.execute_admission_decision(
            rejected_decision_request,
            grant=rejected_decision_grant,
            binding=rejected_admitter_binding,
        )
        self.assertEqual(invalidated["disposition"], "reject")
        self.assertEqual(
            invalidated["candidate_a1_effect"],
            "resolve_exact_a1_as_independently_invalidated",
        )
        self.assertEqual(deep_thaw(self.canonical.state), canonical_before_reject)
        invalid_binding_state = next(
            item
            for item in self.store.list_mission_candidate_a1_bindings("mission.1")
            if item.candidate_id == "candidate.a1-invalidated"
        )
        self.assertEqual(invalid_binding_state.triage_disposition, "admission_ready")
        self.assertEqual(invalid_binding_state.hold_lifecycle, "resolved")
        self.assertEqual(
            invalid_binding_state.admission_rejection["disposition"],
            "independently_invalidated",
        )
        self.assertEqual(
            invalid_binding_state.admission_rejection["objections"][0],
            exact_objection,
        )
        original_authorized_a1 = next(
            item
            for item in self.store.list_mission_candidate_a1_bindings("mission.1")
            if item.candidate_id == "candidate.a1-complete"
        )
        self.assertEqual(original_authorized_a1.hold_lifecycle, "open")
        self.assertEqual(original_authorized_a1.triage_disposition, "admission_ready")
        invalid_candidate_handle = (
            "candidate:candidate.a1-invalidated@"
            f"{invalid_ref['revision']}"
        )
        invalid_triage_handle = (
            f"evidence:{invalid_triage['triage_evidence_ref']['evidence_id']}"
            f"@{invalid_triage['triage_evidence_ref']['revision']}"
        )
        invalid_decision_handle = (
            f"{invalidated['record_ref']['id']}@{invalidated['record_ref']['revision']}"
        )
        invalid_readbacks = MissionInterface.open(
            self.interface.workspace_root,
            expected_project_id=project_id,
            expected_mission_id="mission.1",
        ).reconstruct(
            selected_readback_handles=[
                invalid_candidate_handle,
                invalid_triage_handle,
                invalid_decision_handle,
            ]
        )["selected_readback"]
        self.assertEqual(
            invalid_readbacks[0]["candidate_a1"]["hold_lifecycle"],
            "resolved",
        )
        self.assertEqual(
            invalid_readbacks[0]["candidate_a1"]["triage"]["disposition"],
            "admission_ready",
        )
        self.assertEqual(
            invalid_readbacks[0]["candidate_a1"]["triage"]["retrieval_handle"],
            invalid_triage_handle,
        )
        self.assertEqual(
            invalid_readbacks[0]["candidate_a1"]["admission_rejection"][
                "objections"
            ][0]["exact_objection"],
            "The implication omits the required boundary case.",
        )
        self.assertEqual(
            invalid_readbacks[0]["candidate_a1"]["admission_rejection"][
                "retrieval_handle"
            ],
            invalid_decision_handle,
        )
        self.assertEqual(
            invalid_readbacks[2]["document"]["subject"]["disposition"],
            "reject",
        )
        self.assertEqual(
            invalid_readbacks[2]["document"]["subject"]["objections"][0],
            exact_objection,
        )
        replayed_rejection = self.interface.execute_admission_decision(
            rejected_decision_request,
            grant=rejected_decision_grant,
            binding=rejected_admitter_binding,
        )
        self.assertTrue(replayed_rejection["replayed"])

        decision_ref = AdmissionEvidenceRef(
            decision_result["record_ref"]["id"].removeprefix("evidence:"),
            decision_result["record_ref"]["revision"],
            decision_result["record_ref"]["payload_sha256"],
        )
        terminal_state, admitted_result, canonical_replay = (
            prepare_admitted_research_state(
                deep_thaw(self.canonical.state),
                reopened_decision,
            )
        )
        self.assertFalse(canonical_replay)
        selected_release_sha = "b" * 40
        target_root = Path(self.temporary.name) / "terminal-canonical"
        target_path = target_root / CANONICAL_STATE_REPO_PATH
        target_path.parent.mkdir(parents=True)
        target_path.write_text(
            json.dumps(terminal_state, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        loaded_target = load_canonical_snapshot(
            target_root,
            source_commit=selected_release_sha,
        )
        self.assertTrue(loaded_target.ok, loaded_target.failure)
        self.assertIsNotNone(loaded_target.value)
        target_snapshot = loaded_target.value
        assert target_snapshot is not None

        active_metadata = dict(self.store.read_metadata())
        with self.assertRaises(StaleWriterError):
            self.store.rebind_admitted_result(
                mission_id="mission.1",
                admission_decision_ref=decision_ref,
                source_canonical_authority_digest=str(
                    active_metadata["canonical_authority_digest"]
                ),
                target_canonical_snapshot=target_snapshot,
                selected_release_sha=selected_release_sha,
            )
        self.assertEqual(active_metadata, dict(self.store.read_metadata()))

        continued_with_candidate = self._execute(
            RECORD_STRATEGY,
            {
                "mission_continuation": "continue",
                "integrated_comparison": (
                    "The exact admission-ready Candidate remains the causal basis "
                    "for the terminal canonical cut."
                ),
                "causal_inputs": [
                    {
                        "source": {"id": complete["result"]["record_id"]},
                        "decision_consequence": (
                            "Preserve the exact Candidate through checkpoint and "
                            "perform only the separately authorized canonical rebind."
                        ),
                    }
                ],
                "reconsideration_conditions": [
                    {
                        "condition": (
                            "Reconsider only if the exact Admission Decision or "
                            "terminal canonical delta becomes stale."
                        ),
                        "owner_refs": [
                            {"id": complete["result"]["record_id"]}
                        ],
                    }
                ],
            },
            executive_epoch_id=epoch_id,
        )
        self.assertEqual(
            continued_with_candidate["status"],
            "completed",
            continued_with_candidate,
        )
        checkpointed = self._execute(
            CHECKPOINT,
            {},
            executive_epoch_id=epoch_id,
        )
        self.assertEqual(checkpointed["status"], "completed", checkpointed)
        checkpoint_metadata = dict(self.store.read_metadata())
        lease = self.store.reissue_writer_lease(attest_current_principal())
        self.store.release_writer(
            lease,
            expected_project_commit=int(
                checkpoint_metadata["current_project_commit"]
            ),
            expected_root_digest=str(checkpoint_metadata["current_root_digest"]),
            expected_canonical_authority_digest=str(
                checkpoint_metadata["canonical_authority_digest"]
            ),
            lifecycle="quiesced",
        )
        quiesced_metadata = dict(self.store.read_metadata())
        self.assertIsNone(quiesced_metadata["current_writer_epoch"])
        self.assertEqual(quiesced_metadata["lifecycle"], "quiesced")
        source_authority_digest = str(
            quiesced_metadata["canonical_authority_digest"]
        )

        for label, stale_kwargs in (
            (
                "source",
                {"source_canonical_authority_digest": "0" * 64},
            ),
            (
                "decision",
                {
                    "admission_decision_ref": AdmissionEvidenceRef(
                        decision_ref.evidence_id,
                        decision_ref.revision,
                        "0" * 64,
                    )
                },
            ),
            (
                "target",
                {"selected_release_sha": "c" * 40},
            ),
        ):
            with self.subTest(stale=label):
                before_stale = dict(self.store.read_metadata())
                request = {
                    "mission_id": "mission.1",
                    "admission_decision_ref": decision_ref,
                    "source_canonical_authority_digest": source_authority_digest,
                    "target_canonical_snapshot": target_snapshot,
                    "selected_release_sha": selected_release_sha,
                    **stale_kwargs,
                }
                with self.assertRaises(StaleCommandError):
                    self.store.rebind_admitted_result(**request)
                after_stale = dict(self.store.read_metadata())
                for field in (
                    "current_project_commit",
                    "canonical_authority_digest",
                    "current_root_digest",
                    "transition_head_digest",
                ):
                    self.assertEqual(after_stale[field], before_stale[field])

        def durable_workspace_bytes() -> dict[str, bytes]:
            sqlite_sidecars = {
                Path(f"{self.store.paths.database}-wal"),
                Path(f"{self.store.paths.database}-shm"),
            }
            return {
                path.relative_to(self.store.paths.root).as_posix(): path.read_bytes()
                for path in self.store.paths.root.rglob("*")
                if path.is_file() and path not in sqlite_sidecars
            }

        before_preview_metadata = dict(self.store.read_metadata())
        before_preview_bytes = durable_workspace_bytes()
        preview = self.store.rebind_admitted_result(
            mission_id="mission.1",
            admission_decision_ref=decision_ref,
            source_canonical_authority_digest=source_authority_digest,
            target_canonical_snapshot=target_snapshot,
            selected_release_sha=selected_release_sha,
            dry_run=True,
        )
        self.assertFalse(preview.idempotent)
        self.assertFalse(preview.successor_admitted)
        self.assertEqual(
            preview.project_commit,
            int(quiesced_metadata["current_project_commit"]) + 1,
        )
        self.assertEqual(durable_workspace_bytes(), before_preview_bytes)
        self.assertEqual(dict(self.store.read_metadata()), before_preview_metadata)
        self.assertIsNone(self.store.read_admitted_result("mission.1"))

        rebound = self.store.rebind_admitted_result(
            mission_id="mission.1",
            admission_decision_ref=decision_ref,
            source_canonical_authority_digest=source_authority_digest,
            target_canonical_snapshot=target_snapshot,
            selected_release_sha=selected_release_sha,
        )
        rebound_metadata = dict(self.store.read_metadata())
        self.assertFalse(rebound.idempotent)
        self.assertFalse(rebound.successor_admitted)
        self.assertEqual(
            rebound.project_commit,
            int(quiesced_metadata["current_project_commit"]) + 1,
        )
        self.assertNotEqual(
            rebound.target_canonical_authority_digest,
            source_authority_digest,
        )
        self.assertEqual(
            rebound_metadata["canonical_authority_digest"],
            rebound.target_canonical_authority_digest,
        )
        with self.store._connection(read_only=True) as connection:
            contract = verify_schema_contract(connection, schema_version=10)
            rebind_root6_body, persisted_rebind_root = (
                independent_root6_body_at_commit(
                    connection,
                    schema_contract={
                        "schema_version": contract.schema_version,
                        "migration_set_digest": contract.migration_digest,
                        "schema_object_digest": contract.schema_object_digest,
                    },
                    project_commit_no=rebound.project_commit,
                )
            )
            rebind_result_row = connection.execute(
                "SELECT j.result_payload_digest, r.result_json "
                "FROM transition_journal j JOIN command_result r "
                "ON r.project_commit_no = j.project_commit_no "
                "WHERE j.project_commit_no = ?",
                (rebound.project_commit,),
            ).fetchone()
        self.assertIsNotNone(rebind_result_row)
        assert rebind_result_row is not None
        rebind_result_document = json.loads(str(rebind_result_row["result_json"]))
        self.assertEqual(
            rebind_result_row["result_payload_digest"],
            canonical_payload(rebind_result_document["result"]).sha256,
        )
        self.assertEqual(len(rebind_root6_body), 10)
        self.assertEqual(
            rebind_root6_body,
            {
                "root_digest_version": 6,
                "project_id": rebound.project_id,
                "operating_mode": "mission_runtime",
                "schema_contract": {
                    "schema_version": contract.schema_version,
                    "migration_set_digest": contract.migration_digest,
                    "schema_object_digest": contract.schema_object_digest,
                },
                "project_commit_no": rebound.project_commit,
                "canonical_authority_digest": (
                    rebound.target_canonical_authority_digest
                ),
                "predecessor_project_commit_no": int(
                    quiesced_metadata["current_project_commit"]
                ),
                "predecessor_root_digest": str(
                    quiesced_metadata["current_root_digest"]
                ),
                "predecessor_transition_head_digest": str(
                    quiesced_metadata["transition_head_digest"]
                ),
                "transition_head_digest": rebound.transition_digest,
            },
        )
        for chain_member in (
            "predecessor_project_commit_no",
            "predecessor_root_digest",
            "predecessor_transition_head_digest",
            "transition_head_digest",
        ):
            self.assertIsNotNone(rebind_root6_body[chain_member])
        self.assertEqual(
            independent_root6_digest(rebind_root6_body),
            persisted_rebind_root,
        )
        self.assertEqual(persisted_rebind_root, rebound.root_digest)
        self.assertEqual(
            persisted_rebind_root,
            rebound_metadata["current_root_digest"],
        )
        nulled_rebind_members = set()
        for member, mutation in independent_root6_member_mutations(
            rebind_root6_body
        ):
            with self.subTest(rebind_root6_member=member):
                self.assertNotEqual(
                    independent_root6_digest(mutation),
                    persisted_rebind_root,
                )
            if mutation[member] is None:
                nulled_rebind_members.add(member)
        self.assertEqual(
            nulled_rebind_members,
            {
                "predecessor_project_commit_no",
                "predecessor_root_digest",
                "predecessor_transition_head_digest",
                "transition_head_digest",
            },
        )
        rebound_cut = {
            "project_commit": int(rebound_metadata["current_project_commit"]),
            "current_root_digest": str(rebound_metadata["current_root_digest"]),
            "transition_head_digest": str(
                rebound_metadata["transition_head_digest"]
            ),
            "canonical_authority_digest": str(
                rebound_metadata["canonical_authority_digest"]
            ),
        }
        stale_authority_cut = {
            **rebound_cut,
            "canonical_authority_digest": source_authority_digest,
        }
        before_stale_authorization = dict(self.store.read_metadata())
        before_stale_authorization_bytes = durable_workspace_bytes()
        with (
            mock.patch.object(
                self.store,
                "validate_mission_host_store_cut",
                return_value=stale_authority_cut,
            ),
            self.assertRaises(MissionInterfaceError) as stale_authorization,
        ):
            self.interface.authorize_executive_epoch_from_owner(
                {"expected_cut": stale_authority_cut}
            )
        self.assertEqual(
            stale_authorization.exception.code,
            "mission_host_store_cut_stale",
        )
        self.assertEqual(
            dict(self.store.read_metadata()),
            before_stale_authorization,
        )
        self.assertEqual(
            durable_workspace_bytes(),
            before_stale_authorization_bytes,
        )
        self.assertIsNone(self.store.read_active_executive_epoch("mission.1"))
        self.assertEqual(
            deep_thaw(self.store.read_admitted_result("mission.1")),
            admitted_result,
        )
        self.assertIsNone(self.store.read_active_executive_epoch("mission.1"))
        terminal_a1 = next(
            item
            for item in self.store.list_mission_candidate_a1_bindings("mission.1")
            if item.candidate_id == "candidate.a1-complete"
        )
        self.assertEqual(terminal_a1.hold_lifecycle, "resolved")
        self.assertEqual(terminal_a1.admission_decision_id, decision_ref.evidence_id)
        self.assertEqual(
            deep_thaw(terminal_a1.admitted_result),
            admitted_result,
        )

        before_replay = dict(self.store.read_metadata())
        replayed_rebind = self.store.rebind_admitted_result(
            mission_id="mission.1",
            admission_decision_ref=decision_ref,
            source_canonical_authority_digest=source_authority_digest,
            target_canonical_snapshot=target_snapshot,
            selected_release_sha=selected_release_sha,
        )
        self.assertTrue(replayed_rebind.idempotent)
        self.assertEqual(before_replay, dict(self.store.read_metadata()))
        self.store.verify_integrity()

        copied_database = Path(self.temporary.name) / "tampered-workspace.sqlite3"
        with closing(
            sqlite3.connect(self.store.paths.database)
        ) as source_connection:
            with closing(sqlite3.connect(copied_database)) as copied_connection:
                source_connection.backup(copied_connection)
        with closing(sqlite3.connect(copied_database)) as copied_connection:
            copied_connection.execute(
                "UPDATE transition_journal SET authorization_json = ? "
                "WHERE command_kind = ?",
                ("{}", "rebind_admitted_result"),
            )
            copied_connection.commit()
        tampered_connection = sqlite3.connect(copied_database)
        tampered_connection.row_factory = sqlite3.Row
        try:
            with self.assertRaises(WorkspaceIntegrityError):
                self.store.verify_integrity(
                    _connection_override=tampered_connection,
                )
        finally:
            tampered_connection.close()

        # The canonical successor now owns the exact admitted result.  The
        # same public Strategy operation must reject continuation and accept
        # only its owner-derived closeout reaction.
        self.interface._canonical_snapshot = target_snapshot
        self.goal_thread_id += ":admitted-closeout"
        closeout_epoch = self._authorize()
        self._bind(closeout_epoch)
        current_strategy = read_mission_strategy_head(
            self.store, mission_id="mission.1"
        )
        ordinary_fields = {
            key: deep_thaw(value)
            for key, value in current_strategy.record.document.items()
            if key not in {"schema_version", "kind"}
        }
        ordinary_fields["integrated_comparison"] = (
            "A current admitted result makes another continuation stale."
        )
        ordinary_prepared = prepare_strategy_revision(
            self.store, **ordinary_fields
        )
        before_forbidden_continue = dict(self.store.read_metadata())
        with self.assertRaisesRegex(StaleCommandError, "expected closeout"):
            commit_strategy_revision(
                self.store,
                authority=self.interface._direct_epoch_authority(closeout_epoch),
                prepared=ordinary_prepared,
                lease=self.interface._writer_lease(),
                actor="direct-admitted-strategy-fixture",
                expected_dependency_heads={},
                command_id="direct-strategy-continue-after-admission",
            )
        self.assertEqual(
            dict(self.store.read_metadata()), before_forbidden_continue
        )
        rejected_continue = self._execute(
            RECORD_STRATEGY,
            {
                "mission_continuation": "continue",
                "integrated_comparison": (
                    "The admitted result cannot be replaced by ordinary continuation."
                ),
                "reconsideration_conditions": [
                    {"condition": "Only loss of exact canonical authority."}
                ],
            },
            executive_epoch_id=closeout_epoch,
        )
        self.assertEqual(rejected_continue["status"], "rejected")
        self.assertEqual(
            rejected_continue["error"]["code"],
            "mission_operation_state_conflict",
        )
        self.assertEqual(
            dict(self.store.read_metadata()), before_forbidden_continue
        )
        closeout_input = {
            "mission_continuation": "closeout",
            "integrated_comparison": (
                "The exact admitted result completes the Mission-wide Strategy."
            ),
            "reconsideration_conditions": [
                {"condition": "The exact admitted-result authority becomes stale."}
            ],
        }
        closed = self._execute(
            RECORD_STRATEGY,
            closeout_input,
            executive_epoch_id=closeout_epoch,
        )
        self.assertEqual(closed["status"], "completed", closed)
        self.assertEqual(
            read_mission_strategy_head(
                self.store, mission_id="mission.1"
            ).record.document["mission_continuation"],
            "closeout",
        )
        canonical_checkpoint = self._execute(
            CHECKPOINT,
            {},
            executive_epoch_id=closeout_epoch,
        )
        self.assertEqual(
            canonical_checkpoint["status"], "completed", canonical_checkpoint
        )
        self.store.verify_integrity()

        # Once the admitted-result closeout itself is checkpointed, neither the
        # owner Interface nor a direct Store caller may create another Epoch.
        terminal_metadata = dict(self.store.read_metadata())
        terminal_lease = self.store.reissue_writer_lease(attest_current_principal())
        self.store.release_writer(
            terminal_lease,
            expected_project_commit=int(
                terminal_metadata["current_project_commit"]
            ),
            expected_root_digest=str(terminal_metadata["current_root_digest"]),
            expected_canonical_authority_digest=str(
                terminal_metadata["canonical_authority_digest"]
            ),
            lifecycle="quiesced",
        )
        terminal_cut = self.interface.host_snapshot()["authorization_cut"]

        def terminal_storage_state() -> tuple[dict[str, object], int, int, int]:
            with self.store._connection(read_only=True) as connection:
                metadata = dict(self.store._metadata(connection))
                writer_count = int(
                    connection.execute("SELECT COUNT(*) FROM writer_epoch").fetchone()[0]
                )
                epoch_event_count = int(
                    connection.execute(
                        "SELECT COUNT(*) FROM executive_epoch_event"
                    ).fetchone()[0]
                )
                commit_count = int(
                    connection.execute("SELECT COUNT(*) FROM project_commit").fetchone()[0]
                )
            return metadata, writer_count, epoch_event_count, commit_count

        before_terminal_start = terminal_storage_state()
        with self.assertRaises(MissionInterfaceError) as interface_terminal:
            self.interface.authorize_executive_epoch_from_owner(
                {"expected_cut": terminal_cut}
            )
        self.assertEqual(
            interface_terminal.exception.code,
            "mission_host_store_cut_stale",
        )
        self.assertEqual(terminal_storage_state(), before_terminal_start)
        self.assertIsNone(self.store.read_active_executive_epoch("mission.1"))

        mission = self.store.get_head(
            TypedWorkspaceId(IdentityKind.MISSION, "mission.1")
        )
        self.assertIsNotNone(mission)
        assert mission is not None
        checkpoint = self.store.read_current_mission_checkpoint_summary(
            "mission.1"
        )
        self.assertIsNotNone(checkpoint)
        assert checkpoint is not None
        store_event = prepare_direct_executive_epoch_authorized_event(
            executive_epoch_id="epoch:" + hashlib.sha256(
                b"terminal-admitted-result-store-attempt"
            ).hexdigest(),
            mission_root=OwnerRevisionRef(
                "mission",
                "mission.1",
                mission.reference.revision,
                mission.payload_digest,
            ),
            predecessor_checkpoint={
                "checkpoint_id": checkpoint["checkpoint_id"],
                "payload_sha256": checkpoint["payload_digest"],
            },
        )
        stable_owner = stable_principal_owner_binding(attest_current_principal())
        with self.assertRaisesRegex(
            StaleCommandError,
            "admitted-result closeout checkpoint is terminal",
        ):
            self.store.authorize_executive_epoch(
                mission_id="mission.1",
                executive_epoch_id=store_event.executive_epoch_id,
                event=store_event,
                lease=None,
                command_id="host-authorize-terminal:" + store_event.digest_sha256,
                actor="terminal-admission-store-fixture",
                expected_canonical_authority_digest=str(
                    terminal_cut["canonical_authority_digest"]
                ),
                expected_mission_host_store_cut=terminal_cut,
                atomic_writer_claim_owner=stable_owner,
                atomic_writer_claim_creation_basis=(
                    "owner-authorized-terminal-regression:" + store_event.digest_sha256
                ),
            )
        self.assertEqual(terminal_storage_state(), before_terminal_start)
        self.assertIsNone(self.store.read_active_executive_epoch("mission.1"))

    def test_strategy_authority_precedes_new_semantic_noop_but_not_exact_replay(
        self,
    ) -> None:
        epoch_id = self._authorize()
        self._bind(epoch_id)
        authority = self.interface._direct_epoch_authority(epoch_id)
        self.assertIsNotNone(authority)
        current = read_mission_strategy_head(self.store, mission_id="mission.1")
        closeout_payload = deep_thaw(current.record.document)
        closeout_payload["mission_continuation"] = "closeout"
        closeout_payload["integrated_comparison"] = (
            "Retained legacy unsolved closeout fixture."
        )
        closeout_payload["reconsideration_conditions"] = [
            {"condition": "Explicit owner restart requires reconsideration.", "owner_refs": []}
        ]
        write_args = {
            "executive_epoch_id": epoch_id,
            "mission_id": "mission.1",
            "strategy_id": str(closeout_payload["strategy_id"]),
            "payload": closeout_payload,
            "expected_head_revision": current.revision,
            "expected_head_payload_digest": current.record.payload_sha256,
            "dependency_heads": {},
            "lease": self.interface._writer_lease(),
            "actor": "legacy-closeout-fixture",
            "command_id": "legacy-closeout-original-command",
            "expected_canonical_authority_digest": (
                authority.canonical_authority_digest
            ),
        }

        rejected = self._execute(
            RECORD_STRATEGY,
            {
                "mission_continuation": "closeout",
                "integrated_comparison": "No admitted result authorizes closeout.",
                "reconsideration_conditions": [
                    {"condition": "A future exact admitted result exists."}
                ],
            },
            executive_epoch_id=epoch_id,
        )
        self.assertEqual(rejected["status"], "rejected")
        self.assertEqual(
            rejected["error"]["code"], "mission_operation_state_conflict"
        )

        # Materialize only the historical state that an older accepted writer
        # could have produced.  Removing the patch restores current authority
        # enforcement for replay and semantic-no-op checks below.
        with mock.patch(
            "research_core.workspace_store._current_admitted_result_from_connection",
            return_value={"historical_fixture": True},
        ):
            original = self.store.commit_strategy_revision(**write_args)
        self.assertFalse(original.replayed)
        after_legacy_write = dict(self.store.read_metadata())

        exact_replay = self.store.commit_strategy_revision(**write_args)
        self.assertTrue(exact_replay.replayed)
        self.assertEqual(dict(self.store.read_metadata()), after_legacy_write)

        retained = read_mission_strategy_head(self.store, mission_id="mission.1")
        new_noop_args = {
            **write_args,
            "expected_head_revision": retained.revision,
            "expected_head_payload_digest": retained.record.payload_sha256,
            "command_id": "legacy-closeout-new-command",
        }
        with self.assertRaisesRegex(StaleCommandError, "expected continue"):
            self.store.commit_strategy_revision(**new_noop_args)
        self.assertEqual(dict(self.store.read_metadata()), after_legacy_write)

        orientation = self.interface.executive_orientation()
        self.assertEqual(
            orientation["current_strategy"]["mission_continuation"], "closeout"
        )
        self.assertTrue(orientation["retrieval"]["available_modes"])
        self.assertEqual(
            orientation["retrieval"]["usage_call"]["input"]["for_operation"],
            "retrieve",
        )
        rejected_checkpoint = self._execute(
            CHECKPOINT, {}, executive_epoch_id=epoch_id
        )
        self.assertEqual(rejected_checkpoint["status"], "rejected")
        self.assertIn(
            "historical unsolved Strategy closeout",
            rejected_checkpoint["error"]["message"],
        )
        reopened = self._execute(
            RECORD_STRATEGY,
            {
                "mission_continuation": "continue",
                "integrated_comparison": (
                    "Explicit reconsideration reopens the unsolved historical closeout."
                ),
                "reconsideration_conditions": [
                    {"condition": "Reconsider when the current discriminator resolves."}
                ],
            },
            executive_epoch_id=epoch_id,
        )
        self.assertEqual(reopened["status"], "completed", reopened)
        self.assertEqual(
            read_mission_strategy_head(
                self.store, mission_id="mission.1"
            ).record.document["mission_continuation"],
            "continue",
        )

    def test_semantic_validation_failures_are_scoped_and_write_nothing(self) -> None:
        before_absent = dict(self.store.read_metadata())
        absent = self._execute(
            ORIENT,
            {},
            executive_epoch_id="epoch.absent",
        )
        self.assertEqual(absent["status"], "rejected")
        self.assertEqual(absent["error"]["code"], "executive_epoch_unavailable")
        self.assertEqual(absent["error"]["property"], "executive_authority")
        self.assertEqual(absent["error"]["failure_scope"], "goal")
        self.assertNotIn("location", absent["error"])
        self.assertEqual(before_absent, dict(self.store.read_metadata()))

        epoch_id = self._authorize()
        self._bind(epoch_id)
        before_invalid = dict(self.store.read_metadata())
        invalid = self._execute(
            INTERPRET_MATERIAL,
            {
                "evidence_id": "evidence:evidence.must-not-write",
                "capture_scopes": [
                    {
                        "captured_material_id": "capture:raw-capture:" + ("a" * 48),
                        "artifact_ordinal": 0,
                        "exact_scope": "The complete artifact.",
                        "coverage": "complete_artifact",
                    }
                ],
                "interpretation": "Validation must reject this before persistence.",
                "scope": "The selected artifact only.",
                "strength": "heuristic",
                "limitations": [],
                "significance": "No effect is permitted.",
                "unexpected_second_meaning": "forbidden",
            },
            executive_epoch_id=epoch_id,
        )
        self.assertEqual(invalid["status"], "rejected")
        self.assertEqual(
            invalid["error"]["code"], "mission_operation_request_invalid"
        )
        self.assertEqual(invalid["error"]["property"], "request_shape")
        self.assertEqual(invalid["error"]["failure_scope"], "call")
        self.assertEqual(
            invalid["error"]["location"],
            "$.input.unexpected_second_meaning",
        )
        self.assertEqual(before_invalid, dict(self.store.read_metadata()))
        self.assertEqual(
            self.store.list_mission_evidence_meaning_heads("mission.1"), ()
        )

        invalid_context = self._execute(
            RECORD_CONTEXT,
            {
                "context_id": "missing-context-prefix",
                "subject": "One invalid Context request.",
                "question": "Will it fail before owner resolution?",
                "material": [
                    {
                        "id": "mission:mission.1",
                        "why": "It otherwise supplies valid current owner ground.",
                    }
                ],
            },
            executive_epoch_id=epoch_id,
        )
        self.assertEqual(invalid_context["status"], "rejected")
        self.assertEqual(
            invalid_context["error"]["code"],
            "mission_operation_request_invalid",
        )
        self.assertEqual(invalid_context["error"]["property"], "request_shape")
        self.assertTrue(
            invalid_context["error"]["message"].startswith("$.input.context_id:")
        )
        self.assertEqual(
            invalid_context["error"]["location"],
            "$.input.context_id",
        )
        self.assertEqual(before_invalid, dict(self.store.read_metadata()))

    def test_systemic_store_integrity_failure_is_unavailable_and_write_free(self) -> None:
        epoch_id = self._authorize()
        self._bind(epoch_id)
        capture_id = self._capture_pending_material(epoch_id)
        before = dict(self.store.read_metadata())
        with mock.patch(
            "research_core.mission_interface.read_raw_capture_artifact",
            side_effect=WorkspaceIntegrityError(
                "simulated systemic direct Store/CAS corruption"
            ),
        ):
            result = self._execute(
                RETRIEVE,
                {
                    "mode": "read",
                    "purpose": "Read the exact captured bytes through Store and CAS.",
                    "ids": [f"capture-artifact:{capture_id}#0"],
                },
                executive_epoch_id=epoch_id,
            )
        self.assertEqual(result["status"], "unavailable")
        self.assertEqual(result["error"]["code"], "mission_operation_unavailable")
        self.assertEqual(result["error"]["property"], "interface_availability")
        self.assertEqual(result["error"]["failure_scope"], "operation")
        self.assertEqual(result["error"]["correction"], "repair_owner_interface")
        self.assertEqual(before, dict(self.store.read_metadata()))

    def test_quarantined_artifact_is_local_unavailable_and_research_continues(
        self,
    ) -> None:
        epoch_id = self._authorize()
        self._bind(epoch_id)
        quarantined_bytes = b"Quarantined operational transcript fixture.\n"
        source = Path(self.temporary.name) / "quarantined-transcript.jsonl"
        source.write_bytes(quarantined_bytes)
        record = prepare_raw_capture(
            self.store,
            cas=self.cas,
            mission_id="mission.1",
            executive_epoch_id=epoch_id,
            capture_kind="output",
            observation_id="observation.quarantined-local-material",
            assignment_id="child-thread:quarantined-local-material",
            provenance={"kind": "direct_interface_test_fixture"},
            completion={"lifecycle": "completed"},
            artifacts=(
                RawCaptureArtifactFileInput(
                    role="evidence_only",
                    logical_name="scratch/app-server-events.jsonl",
                    source_path=source.resolve(),
                    sha256=hashlib.sha256(quarantined_bytes).hexdigest(),
                    byte_length=len(quarantined_bytes),
                    media_type="application/x-ndjson",
                    encoding="utf-8",
                    quarantine_reason="opaque_restricted",
                ),
            ),
        )
        commit_raw_capture(
            self.store,
            cas=self.cas,
            record=record,
            lease=self.store.reissue_writer_lease(attest_current_principal()),
            actor="direct-mission-interface-test",
        )
        before = dict(self.store.read_metadata())
        artifact_id = f"capture-artifact:{record.capture_id}#0"

        result = self._execute(
            RETRIEVE,
            {
                "mode": "read",
                "purpose": "Read one exact retained artifact.",
                "ids": [artifact_id],
            },
            executive_epoch_id=epoch_id,
        )

        self.assertEqual(result["status"], "completed", result)
        unavailable_item = result["result"]["items"][0]
        self.assertEqual(unavailable_item["requested_id"], artifact_id)
        self.assertEqual(unavailable_item["status"], "unavailable")
        self.assertEqual(unavailable_item["reason"], "quarantined")
        self.assertIn("other available material", unavailable_item["correction"])
        self.assertEqual(before, dict(self.store.read_metadata()))

    def test_corrupt_integrity_is_systemic_even_with_local_unavailability_labels(
        self,
    ) -> None:
        epoch_id, _exact_text, _exact_binary, record = (
            self._prepare_exact_retrieval_fixture()
        )
        mixed_state = mock.Mock(
            integrity_state="corrupt",
            quarantine_state="quarantined",
            availability_state="missing",
            ordinary_available=False,
        )
        artifact_id = f"capture-artifact:{record.capture_id}#0"
        before = dict(self.store.read_metadata())

        with mock.patch(
            "research_core.workspace_store.BlobRecord",
            return_value=mixed_state,
        ):
            result = self._execute(
                RETRIEVE,
                {
                    "mode": "read",
                    "purpose": "Reject corrupt custody despite local labels.",
                    "ids": [artifact_id],
                },
                executive_epoch_id=epoch_id,
            )

        self.assertEqual(result["status"], "unavailable", result)
        self.assertEqual(result["error"]["code"], "mission_operation_unavailable")
        self.assertEqual(result["error"]["property"], "interface_availability")
        self.assertEqual(result["error"]["failure_scope"], "operation")
        self.assertEqual(result["error"]["correction"], "repair_owner_interface")
        self.assertIn("integrity is not verified", result["error"]["message"])
        self.assertEqual(before, dict(self.store.read_metadata()))

    def test_direct_authorization_rejects_missing_or_drifted_mission_contract(
        self,
    ) -> None:
        current = self.interface._require_mission()
        missing = dict(current)
        del missing["purpose"]
        drifted = json.loads(canonical_json_bytes(current))
        drifted["execution_policy"]["model"] = "different-model"
        for invalid in (missing, drifted):
            with (
                mock.patch.object(
                    self.interface,
                    "_require_mission",
                    return_value=invalid,
                ),
                self.assertRaises(MissionInterfaceError) as raised,
            ):
                self.interface.authorize_executive_epoch_from_owner(
                    {"expected_cut": self.interface.host_snapshot()["authorization_cut"]}
                )
            self.assertEqual(
                raised.exception.code,
                "successor_mission_contract_invalid",
            )

    def test_explicit_epoch_authorization_atomically_claims_quiesced_writer_and_replays_after_crash(
        self,
    ) -> None:
        before = self.store.read_metadata()
        original_epoch = int(before["current_writer_epoch"])
        lease = self.store.reissue_writer_lease(attest_current_principal())
        self.store.release_writer(
            lease,
            expected_project_commit=int(before["current_project_commit"]),
            expected_root_digest=str(before["current_root_digest"]),
            expected_canonical_authority_digest=str(
                before["canonical_authority_digest"]
            ),
            lifecycle="quiesced",
        )
        quiesced = self.store.read_metadata()
        self.assertIsNone(quiesced["current_writer_epoch"])
        self.assertEqual(quiesced["lifecycle"], "quiesced")

        expected_cut = self.interface.host_snapshot()["authorization_cut"]
        with (
            mock.patch.object(
                self.store,
                "_after_commit",
                side_effect=RuntimeError(
                    "simulated crash after atomic authorization commit"
                ),
            ),
            self.assertRaisesRegex(RuntimeError, "simulated crash"),
        ):
            self.interface.authorize_executive_epoch_from_owner(
                {"expected_cut": expected_cut}
            )

        claimed = self.store.read_metadata()
        claimed_epoch = int(claimed["current_writer_epoch"])
        self.assertEqual(claimed_epoch, original_epoch + 1)
        self.assertEqual(claimed["lifecycle"], "active")
        self.assertEqual(
            int(claimed["current_project_commit"]),
            int(quiesced["current_project_commit"]) + 1,
        )
        with self.store.snapshot_connection() as connection:
            writer = connection.execute(
                "SELECT owner, lifecycle, creation_basis FROM writer_epoch WHERE epoch = ?",
                (claimed_epoch,),
            ).fetchone()
        self.assertEqual(
            writer["owner"],
            stable_principal_owner_binding(attest_current_principal()),
        )
        self.assertEqual(writer["lifecycle"], "active")
        self.assertTrue(
            str(writer["creation_basis"]).startswith(
                "owner-authorized-rh-mission-start:"
            )
        )

        authorized = self.interface.authorize_executive_epoch_from_owner(
            {"expected_cut": expected_cut}
        )
        self.assertEqual(authorized["state"], "authorized")
        self.assertEqual(
            int(self.store.read_metadata()["current_writer_epoch"]),
            claimed_epoch,
        )

    def test_quiesced_epoch_authorization_failure_rolls_back_tentative_writer_claim(
        self,
    ) -> None:
        before = self.store.read_metadata()
        self.store.release_writer(
            self.store.reissue_writer_lease(attest_current_principal()),
            expected_project_commit=int(before["current_project_commit"]),
            expected_root_digest=str(before["current_root_digest"]),
            expected_canonical_authority_digest=str(
                before["canonical_authority_digest"]
            ),
            lifecycle="quiesced",
        )
        quiesced = dict(self.store.read_metadata())
        expected_cut = self.interface.host_snapshot()["authorization_cut"]

        def counts() -> tuple[int, int, int, int]:
            with self.store.snapshot_connection() as connection:
                return tuple(
                    int(connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
                    for table in (
                        "writer_epoch",
                        "executive_epoch_event",
                        "transition_journal",
                        "command_result",
                    )
                )

        before_counts = counts()
        with (
            mock.patch(
                "research_core.workspace_store._validate_successor_epoch_transaction",
                side_effect=RuntimeError(
                    "simulated failure after tentative writer claim"
                ),
            ),
            self.assertRaisesRegex(RuntimeError, "tentative writer claim"),
        ):
            self.interface.authorize_executive_epoch_from_owner(
                {"expected_cut": expected_cut}
            )

        self.assertEqual(dict(self.store.read_metadata()), quiesced)
        self.assertEqual(counts(), before_counts)
        self.assertIsNone(self.store.read_active_executive_epoch("mission.1"))

    def test_explicit_epoch_authorization_does_not_reclaim_revoked_writer(
        self,
    ) -> None:
        before = self.store.read_metadata()
        lease = self.store.reissue_writer_lease(attest_current_principal())
        self.store.release_writer(
            lease,
            expected_project_commit=int(before["current_project_commit"]),
            expected_root_digest=str(before["current_root_digest"]),
            expected_canonical_authority_digest=str(
                before["canonical_authority_digest"]
            ),
            lifecycle="revoked",
        )

        with self.assertRaisesRegex(
            StaleWriterError,
            "only a cleanly quiesced Workspace",
        ):
            self.interface.authorize_executive_epoch_from_owner(
                {"expected_cut": self.interface.host_snapshot()["authorization_cut"]}
            )

        blocked = self.store.read_metadata()
        self.assertEqual(blocked["lifecycle"], "blocked_read_only")
        self.assertIsNone(blocked["current_writer_epoch"])

    def test_reconstruct_v2_without_checkpoint_opens_from_current_genesis_heads(
        self,
    ) -> None:
        reconstruction = self.interface.reconstruct()
        self.assertEqual(
            set(reconstruction),
            {
                "schema_version",
                "project_id",
                "mission_id",
                "observed_project_commit",
                "recovery_opening",
                "latest_executive_epoch",
            },
        )
        opening = reconstruction["recovery_opening"]
        self.assertEqual(
            set(opening),
            {
                "cut",
                "owner_deltas",
                "open_candidate_a1",
                "hooks",
                "opportunity_portfolio",
                "raw_captures",
                "post_cut_transitions",
                "admitted_result",
            },
        )
        self.assertEqual(opening["open_candidate_a1"], [])
        self.assertIsNone(opening["admitted_result"])
        self.assertIsNone(opening["cut"])
        self.assertIsNone(reconstruction["latest_executive_epoch"])
        self.assertEqual(opening["raw_captures"], [])
        deltas = opening["owner_deltas"]
        self.assertEqual(
            {
                item["current_reference"]["kind"]
                for item in deltas
            },
            {"mission", "branch", "strategy"},
        )
        for delta in deltas:
            self.assertEqual(
                set(delta),
                {
                    "before_reference",
                    "current_reference",
                    "relation",
                    "retrieval_handle",
                    "decision_connections",
                    "summary",
                },
            )
            self.assertIsNone(delta["before_reference"])
            self.assertEqual(delta["relation"], "no_prior_cut")
            current = delta["current_reference"]
            self.assertEqual(
                delta["retrieval_handle"],
                f"{current['kind']}:{current['identity']}@{current['revision']}",
            )
        branch_delta = next(
            item
            for item in deltas
            if item["current_reference"]["kind"] == "branch"
        )
        mission_delta = next(
            item
            for item in deltas
            if item["current_reference"]["kind"] == "mission"
        )
        strategy_delta = next(
            item
            for item in deltas
            if item["current_reference"]["kind"] == "strategy"
        )
        self.assertEqual(
            set(mission_delta["summary"]),
            {
                "lifecycle",
                "effective",
                "fence_reason",
                "fenced_at",
                "autonomous",
                "strategy_ids",
                "purpose",
                "execution_policy",
            },
        )
        self.assertEqual(
            mission_delta["summary"]["purpose"]["objective"],
            SUCCESSOR_MISSION_OBJECTIVE,
        )
        self.assertEqual(
            mission_delta["summary"]["execution_policy"],
            json.loads(canonical_json_bytes(successor_mission_contract()))[
                "execution_policy"
            ],
        )
        self.assertEqual(
            set(strategy_delta["summary"]),
            {"mission_continuation", "integrated_comparison", "formal_requests"},
        )
        self.assertEqual(strategy_delta["summary"]["formal_requests"], [])
        self.assertEqual(
            set(branch_delta["summary"]),
            {"question", "leverage_fingerprint", "target_hook"},
        )
        for removed in (
            "scoped_failures",
            "non_exclusions",
            "retained_residue",
            "composition_interfaces",
            "recombination_interfaces",
            "revival_conditions",
        ):
            self.assertNotIn(removed, branch_delta["summary"])
        self.assertIn(
            "/attention_actions/0/branch_ref",
            {
                item["json_pointer"]
                for item in branch_delta["decision_connections"]
            },
        )
        hooks = opening["hooks"]
        self.assertEqual(
            set(hooks),
            {
                "unresolved",
                "revival",
                "recombination",
                "reconsideration",
                "reversal",
            },
        )
        self.assertTrue(hooks["reconsideration"])
        self.assertEqual(hooks["reversal"], [])
        portfolio = opening["opportunity_portfolio"]
        self.assertIn("reconsideration_conditions", portfolio)
        self.assertIn("reversal_conditions", portfolio)
        transitions = opening["post_cut_transitions"]
        self.assertEqual(
            [item["project_commit"] for item in transitions],
            list(range(1, reconstruction["observed_project_commit"] + 1)),
        )
        self.assertTrue(
            all(
                set(item)
                == {
                    "project_commit",
                    "command_kind",
                    "changed_owner_refs",
                    "retired_owner_refs",
                    "evidence_head_advances",
                    "auxiliary_facts",
                }
                for item in transitions
            )
        )
        self._assert_compact_recovery_projection(reconstruction)

    def test_reconstruct_v2_projects_cut_late_capture_and_exact_owner_deltas(
        self,
    ) -> None:
        predecessor_epoch = self._authorize()
        self._bind(predecessor_epoch)
        pending_capture_id = self._capture_pending_material(predecessor_epoch)
        checkpointed = self.interface.execute_semantic_operation(
            self._request(CHECKPOINT, {}),
            executive_epoch_id=predecessor_epoch,
        )
        self.assertEqual(checkpointed["status"], "completed", checkpointed)
        checkpoint = self.store.read_continuation_checkpoint(
            executive_epoch_id=predecessor_epoch
        )
        self.assertIsNotNone(checkpoint)
        assert checkpoint is not None

        late = self.interface.capture_native_material_observation(
            {
                "observationId": "observation.recovery.late",
                "materialKind": "output",
                "content": "Exact late material remains uninterpreted custody.",
                "rootThreadId": self.goal_thread_id,
                "parentThreadId": self.goal_thread_id,
                "childThreadId": "child-thread:recovery-late",
            },
            executive_epoch_id=predecessor_epoch,
        )
        late_capture_id = str(late["material_id"]).partition(":")[2]

        self.goal_thread_id = "goal-thread:direct-mission-interface-successor"
        self.goal_workspace = self.goal_workspace.parent / "successor"
        self.goal_workspace.mkdir()
        successor_epoch = self._authorize()
        self._bind(successor_epoch)
        context = self.interface.execute_semantic_operation(
            self._request(
                RECORD_CONTEXT,
                {
                    "context_id": "context:context.after.cut",
                    "subject": "Exact post-cut Context.",
                    "question": "What changed after the checkpoint cut?",
                    "material": [
                        {
                            "id": "mission:mission.1",
                            "why": "The Mission fixes the exact recovery scope.",
                        }
                    ],
                },
            ),
            executive_epoch_id=successor_epoch,
        )
        self.assertEqual(context["status"], "completed", context)
        candidate = self.interface.execute_semantic_operation(
            self._request(
                RECORD_CANDIDATE,
                {
                    "candidate_id": "candidate:candidate.after.cut",
                    "proposal_kind": "mechanism",
                    "exact_statement": (
                        "A cut-local obstruction may isolate the next exact test."
                    ),
                    "mechanism": "Compare the preserved finite identities at the cut.",
                    "standing": {
                        "status": "open",
                        "basis": "The mechanism remains an authored research proposal.",
                    },
                    "gaps": ["The comparison has not been extended uniformly."],
                    "objections": ["The current evidence is only cut-local."],
                    "circularity_risks": [
                        "The comparison must not assume its target identity."
                    ],
                },
            ),
            executive_epoch_id=successor_epoch,
        )
        self.assertEqual(candidate["status"], "completed", candidate)
        evidence = self.interface.execute_semantic_operation(
            self._request(
                INTERPRET_MATERIAL,
                {
                    "evidence_id": "evidence:evidence.after.cut",
                    "capture_scopes": [
                        {
                            "captured_material_id": f"capture:{pending_capture_id}",
                            "artifact_ordinal": 0,
                            "exact_scope": "Every byte in the pending output artifact.",
                            "coverage": "complete_artifact",
                        }
                    ],
                    "interpretation": (
                        "The exact pending output preserves one cut-local obstruction."
                    ),
                    "scope": "The one preserved output artifact only.",
                    "strength": "heuristic",
                    "limitations": ["No uniform theorem follows."],
                    "significance": (
                        "The obstruction can inform the next exact comparison."
                    ),
                },
            ),
            executive_epoch_id=successor_epoch,
        )
        self.assertEqual(evidence["status"], "completed", evidence)
        strategy = self.interface.execute_semantic_operation(
            self._request(
                RECORD_STRATEGY,
                {
                    "mission_continuation": "continue",
                    "integrated_comparison": (
                        "The new Context is current while the mathematical Branch "
                        "is unchanged."
                    ),
                    "causal_inputs": [
                        {
                            "source": {
                                "id": evidence["result"]["records"][0]["record_id"],
                            },
                            "decision_consequence": (
                                "Retain the exact comparison as the next discriminator."
                            ),
                        }
                    ],
                    "attention_actions": [
                        {
                            "branch": {
                                "id": "branch:branch.theta",
                            },
                            "attention": "active",
                            "consequence": "Continue the exact construction.",
                        }
                    ],
                    "reconsideration_conditions": [
                        {
                            "condition": (
                                "The exact construction closes or exposes its residual."
                            ),
                            "owner_refs": [],
                        }
                    ],
                    "reversal_conditions": [
                        {
                            "condition": (
                                "A cited contradiction refutes the construction at scope."
                            ),
                            "owner_refs": [],
                        }
                    ],
                },
            ),
            executive_epoch_id=successor_epoch,
        )
        self.assertEqual(strategy["status"], "completed", strategy)

        reconstruction = self.interface.reconstruct()
        opening = reconstruction["recovery_opening"]
        self.assertEqual(opening["cut"]["document"], checkpoint["document"])
        deltas = {
            (
                item["current_reference"]["kind"],
                item["current_reference"]["identity"],
            ): item
            for item in opening["owner_deltas"]
        }
        self.assertEqual(deltas[("mission", "mission.1")]["relation"], "unchanged")
        self.assertEqual(deltas[("branch", "branch.theta")]["relation"], "unchanged")
        self.assertEqual(
            deltas[("strategy", "strategy.theta.1")]["relation"],
            "advanced",
        )
        new_context = deltas[("context", "context.after.cut")]
        self.assertEqual(new_context["relation"], "new")
        self.assertIsNone(new_context["before_reference"])
        self.assertEqual(set(new_context["summary"]), {"purpose", "question"})
        self.assertNotIn("known_omissions", new_context["summary"])
        self.assertNotIn("restricted_uses", new_context["summary"])
        candidate_delta = deltas[("candidate", "candidate.after.cut")]
        self.assertEqual(candidate_delta["relation"], "new")
        self.assertEqual(
            set(candidate_delta["summary"]),
            {"proposal_kind", "exact_statement", "standing"},
        )
        for removed in (
            "mechanism",
            "gaps",
            "objections",
            "circularity_risks",
            "limitations",
            "non_inferences",
        ):
            self.assertNotIn(removed, candidate_delta["summary"])
        evidence_delta = deltas[("evidence", "evidence.after.cut")]
        self.assertEqual(evidence_delta["relation"], "new")
        self.assertEqual(
            set(evidence_delta["summary"]),
            {"subject", "exact_scope", "rigor", "limitations", "non_inferences"},
        )
        self.assertEqual(
            set(evidence_delta["summary"]["subject"]),
            {"statement", "semantic_role", "decision_consequence"},
        )
        self.assertNotIn("mission_id", evidence_delta["summary"]["subject"])
        self.assertIn(
            "/causal_inputs/0/source_ref",
            {
                item["json_pointer"]
                for item in evidence_delta["decision_connections"]
            },
        )
        self.assertEqual(
            set(opening["hooks"]),
            {
                "unresolved",
                "revival",
                "recombination",
                "reconsideration",
                "reversal",
            },
        )
        self.assertEqual(len(opening["hooks"]["reconsideration"]), 1)
        self.assertEqual(len(opening["hooks"]["reversal"]), 1)
        self.assertEqual(
            {
                item["semantic_field"] for item in opening["hooks"]["unresolved"]
            },
            {"gaps", "objections", "circularity_risks"},
        )

        captures = {
            item["capture_id"]: item for item in opening["raw_captures"]
        }
        self.assertEqual(
            captures[pending_capture_id]["late_classification"],
            "at_or_before_terminal",
        )
        self.assertTrue(captures[pending_capture_id]["artifacts"][0]["pending_at_cut"])
        self.assertFalse(
            captures[pending_capture_id]["artifacts"][0]["pending_current"]
        )
        self.assertEqual(
            captures[late_capture_id]["late_classification"],
            "after_checkpoint_terminal",
        )
        self.assertIsNone(
            captures[late_capture_id]["artifacts"][0]["pending_at_cut"]
        )
        self.assertTrue(
            captures[late_capture_id]["artifacts"][0]["pending_current"]
        )
        self.assertTrue(
            all(
                item["project_commit"] > checkpoint["project_commit_no"]
                for item in opening["post_cut_transitions"]
            )
        )
        self.assertEqual(reconstruction["latest_executive_epoch"]["state"], "bound")
        self.assertEqual(
            set(reconstruction["latest_executive_epoch"]),
            {
                "executive_epoch_id",
                "state",
                "goal_thread_id",
                "last_event_project_commit",
                "checkpoint_ref",
                "reconciliation",
            },
        )
        self.assertEqual(
            reconstruction["latest_executive_epoch"]["goal_thread_id"],
            self.goal_thread_id,
        )
        self.assertIsNone(
            reconstruction["latest_executive_epoch"]["checkpoint_ref"]
        )
        self._assert_compact_recovery_projection(reconstruction)

    def test_retrieve_reads_exact_raw_artifact_bytes_without_evidence_meaning(self) -> None:
        epoch_id, exact_text, exact_binary, record = (
            self._prepare_exact_retrieval_fixture()
        )
        capture_id = record.capture_id
        descriptor_id = f"capture:{capture_id}"
        text_id = f"capture-artifact:{capture_id}#0"
        binary_id = f"capture-artifact:{capture_id}#1"
        response = self.interface.execute_semantic_operation(
            self._request(
                RETRIEVE,
                {
                    "mode": "read",
                    "purpose": "Read the exact untrusted worker material.",
                    "ids": [descriptor_id, text_id, binary_id],
                },
            ),
            executive_epoch_id=epoch_id,
        )
        self.assertEqual(response["status"], "completed", response)
        items = {item["id"]: item for item in response["result"]["items"]}
        self.assertEqual(set(items), {descriptor_id, text_id, binary_id})
        self.assertNotIn("literal research text", items[descriptor_id]["readable_content"])

        text_transfer = json.loads(items[text_id]["readable_content"])
        self.assertEqual(
            text_transfer,
            {
                "representation": "text",
                "media_type": "application/x-ndjson",
                "text_encoding": "utf-8",
                "content": exact_text.decode("utf-8"),
            },
        )
        self.assertEqual(
            text_transfer["content"].encode(text_transfer["text_encoding"]),
            exact_text,
        )
        binary_transfer = json.loads(items[binary_id]["readable_content"])
        self.assertEqual(binary_transfer["representation"], "base64_rfc4648")
        self.assertEqual(binary_transfer["media_type"], "application/octet-stream")
        self.assertEqual(
            base64.b64decode(binary_transfer["content"], validate=True),
            exact_binary,
        )
        self.assertNotIn("blob_sha256", binary_transfer)
        self.assertNotIn("byte_length", binary_transfer)

        neutral_text = RawCaptureArtifactContent(
            capture_id="raw-capture:neutral-text",
            mission_id="mission.1",
            artifact=RawCaptureArtifactDescriptor(
                ordinal=0,
                role="accepted_output",
                logical_name="output/formal-result.txt",
                blob_sha256=hashlib.sha256(b"neutral exact text\n").hexdigest(),
            ),
            media_type="application/octet-stream",
            encoding=None,
            content_bytes=b"neutral exact text\n",
        )
        self.assertEqual(
            self.interface._lossless_raw_artifact_transfer(neutral_text),
            {
                "representation": "text",
                "media_type": "application/octet-stream",
                "text_encoding": "utf-8",
                "content": "neutral exact text\n",
            },
        )

        with self.assertRaisesRegex(StaleCommandError, "another Mission"):
            self.store.read_raw_capture_artifact_bytes(
                cas=self.cas,
                mission_id="mission.another",
                capture_id=capture_id,
                artifact_ordinal=0,
            )

        binary_path = self.cas.path_for_digest(record.artifacts[1].blob_sha256)
        os.chmod(
            binary_path,
            stat.S_IREAD | stat.S_IWRITE if os.name == "nt" else 0o600,
        )
        binary_path.write_bytes(b"tampered")
        corrupt = self.interface.execute_semantic_operation(
            self._request(
                RETRIEVE,
                {
                    "mode": "read",
                    "purpose": "Reject changed custody bytes.",
                    "ids": [binary_id],
                },
            ),
            executive_epoch_id=epoch_id,
        )
        self.assertEqual(corrupt["status"], "unavailable", corrupt)
        self.assertEqual(corrupt["error"]["code"], "mission_operation_unavailable")

        binary_path.write_bytes(exact_binary)
        text_path = self.cas.path_for_digest(record.artifacts[0].blob_sha256)
        os.chmod(
            text_path,
            stat.S_IREAD | stat.S_IWRITE if os.name == "nt" else 0o600,
        )
        text_path.unlink()
        missing = self.interface.execute_semantic_operation(
            self._request(
                RETRIEVE,
                {
                    "mode": "read",
                    "purpose": "Reject absent custody bytes.",
                    "ids": [text_id],
                },
            ),
            executive_epoch_id=epoch_id,
        )
        self.assertEqual(missing["status"], "unavailable", missing)
        self.assertEqual(missing["error"]["code"], "mission_operation_unavailable")

        self.assertEqual(
            self.store.list_mission_evidence_meaning_heads("mission.1"), ()
        )

    def test_reconstruct_selected_readback_is_complete_and_lossless(self) -> None:
        _epoch_id, exact_text, exact_binary, record = (
            self._prepare_exact_retrieval_fixture()
        )
        compact = self.interface.reconstruct()
        self.assertNotIn("selected_readback", compact)
        strategy_delta = next(
            item
            for item in compact["recovery_opening"]["owner_deltas"]
            if item["current_reference"]["kind"] == "strategy"
        )
        capture_id = record.capture_id
        handles = (
            strategy_delta["retrieval_handle"],
            f"capture-artifact:{capture_id}#0",
            f"capture-artifact:{capture_id}#1",
        )

        reconstruction = self.interface.reconstruct(
            selected_readback_handles=handles
        )
        selected = reconstruction["selected_readback"]
        self.assertEqual(
            [item["retrieval_handle"] for item in selected],
            list(handles),
        )
        strategy = selected[0]
        self.assertEqual(strategy["readback_kind"], "current_owner_document")
        self.assertEqual(
            strategy["current_reference"],
            strategy_delta["current_reference"],
        )
        self.assertEqual(strategy["document"]["kind"], "mission_strategy")
        self.assertEqual(
            strategy["document"]["mission_continuation"],
            strategy_delta["summary"]["mission_continuation"],
        )
        self.assertIn("selected_bets", strategy["document"])

        text_readback, binary_readback = selected[1:]
        for item, ordinal, exact_bytes in (
            (text_readback, 0, exact_text),
            (binary_readback, 1, exact_binary),
        ):
            self.assertEqual(item["readback_kind"], "raw_capture_artifact")
            self.assertEqual(item["capture_document"]["capture_id"], capture_id)
            self.assertEqual(item["capture_document"]["mission_id"], "mission.1")
            self.assertIsNotNone(item["capture_document"]["created_at"])
            self.assertEqual(item["artifact_metadata"]["ordinal"], ordinal)
            self.assertEqual(item["artifact_metadata"]["byte_length"], len(exact_bytes))
            self.assertEqual(
                item["artifact_metadata"]["blob_sha256"],
                hashlib.sha256(exact_bytes).hexdigest(),
            )
        self.assertEqual(
            text_readback["content"],
            {
                "representation": "text",
                "media_type": "application/x-ndjson",
                "text_encoding": "utf-8",
                "content": exact_text.decode("utf-8"),
            },
        )
        self.assertEqual(
            base64.b64decode(binary_readback["content"]["content"], validate=True),
            exact_binary,
        )
        self.assertEqual(
            binary_readback["content"]["representation"],
            "base64_rfc4648",
        )
        self.assertEqual(
            self.store.list_mission_evidence_meaning_heads("mission.1"), ()
        )

        with self.assertRaises(MissionInterfaceError) as inexact:
            self.interface.reconstruct(
                selected_readback_handles=("strategy:strategy.theta.1",)
            )
        self.assertEqual(
            inexact.exception.code,
            "mission_interface_request_invalid",
        )
        with self.assertRaises(StaleCommandError):
            self.interface.reconstruct(
                selected_readback_handles=("strategy:strategy.theta.1@99",)
            )

    def test_reconstruct_selected_readback_preserves_historical_owner_handles(
        self,
    ) -> None:
        epoch_id, _exact_text, _exact_binary, _record = (
            self._prepare_exact_retrieval_fixture()
        )
        compact = self.interface.reconstruct()
        strategy_delta = next(
            item
            for item in compact["recovery_opening"]["owner_deltas"]
            if item["current_reference"]["kind"] == "strategy"
        )
        historical_handle = strategy_delta["retrieval_handle"]
        historical_reference = strategy_delta["current_reference"]

        advanced = self._execute(
            RECORD_STRATEGY,
            {
                "mission_continuation": "continue",
                "integrated_comparison": (
                    "A later Strategy revision makes the exact prior revision "
                    "historical without making its handle unreadable."
                ),
                "reconsideration_conditions": [
                    {
                        "condition": (
                            "Reconsider after the next exact mathematical result."
                        )
                    }
                ],
            },
            executive_epoch_id=epoch_id,
        )
        self.assertEqual(advanced["status"], "completed", advanced)

        reconstruction = self.interface.reconstruct(
            selected_readback_handles=(historical_handle,)
        )
        selected = reconstruction["selected_readback"]
        self.assertEqual(len(selected), 1)
        self.assertEqual(selected[0]["retrieval_handle"], historical_handle)
        self.assertEqual(
            selected[0]["readback_kind"],
            "historical_owner_document",
        )
        self.assertEqual(
            selected[0]["historical_reference"],
            historical_reference,
        )
        self.assertEqual(selected[0]["document"]["kind"], "mission_strategy")

    def test_reconstruct_selected_readback_reports_one_unavailable_artifact_locally(
        self,
    ) -> None:
        epoch_id = self._authorize()
        self._bind(epoch_id)
        clean_bytes = b"Readable mathematical output.\n"
        quarantined_bytes = b"Quarantined operational transcript.\n"
        clean_source = Path(self.temporary.name) / "formal-result.txt"
        quarantined_source = Path(self.temporary.name) / "app-server-events.jsonl"
        clean_source.write_bytes(clean_bytes)
        quarantined_source.write_bytes(quarantined_bytes)
        record = prepare_raw_capture(
            self.store,
            cas=self.cas,
            mission_id="mission.1",
            executive_epoch_id=epoch_id,
            capture_kind="output",
            observation_id="observation.selected-readback-local-unavailability",
            assignment_id="child-thread:selected-readback-local-unavailability",
            provenance={"kind": "direct_interface_test_fixture"},
            completion={"lifecycle": "completed"},
            artifacts=(
                RawCaptureArtifactFileInput(
                    role="accepted_output",
                    logical_name="output/formal-result.txt",
                    source_path=clean_source.resolve(),
                    sha256=hashlib.sha256(clean_bytes).hexdigest(),
                    byte_length=len(clean_bytes),
                    media_type="text/plain",
                    encoding="utf-8",
                ),
                RawCaptureArtifactFileInput(
                    role="evidence_only",
                    logical_name="scratch/app-server-events.jsonl",
                    source_path=quarantined_source.resolve(),
                    sha256=hashlib.sha256(quarantined_bytes).hexdigest(),
                    byte_length=len(quarantined_bytes),
                    media_type="application/x-ndjson",
                    encoding="utf-8",
                    quarantine_reason="opaque_restricted",
                ),
            ),
        )
        commit_raw_capture(
            self.store,
            cas=self.cas,
            record=record,
            lease=self.store.reissue_writer_lease(attest_current_principal()),
            actor="direct-mission-interface-test",
        )
        handles = (
            f"capture-artifact:{record.capture_id}#0",
            f"capture-artifact:{record.capture_id}#1",
        )
        before = dict(self.store.read_metadata())

        selected = self.interface.reconstruct(
            selected_readback_handles=handles
        )["selected_readback"]

        self.assertEqual(
            [item["retrieval_handle"] for item in selected],
            list(handles),
        )
        self.assertEqual(selected[0]["readback_kind"], "raw_capture_artifact")
        self.assertEqual(selected[0]["content"]["content"], clean_bytes.decode())
        unavailable = selected[1]
        self.assertEqual(
            unavailable["readback_kind"], "raw_capture_artifact_unavailable"
        )
        self.assertNotIn("content", unavailable)
        self.assertEqual(unavailable["artifact_metadata"]["ordinal"], 1)
        self.assertEqual(
            unavailable["availability"],
            {
                "status": "unavailable",
                "code": "mission_material_unavailable",
                "disposition": "quarantined",
                "failure_scope": "call",
                "correction": "continue_other_material_and_report_exact_artifact",
            },
        )
        self.assertEqual(before, dict(self.store.read_metadata()))

    def test_formal_request_selector_projects_resolves_and_rejects_stale_bet(
        self,
    ) -> None:
        epoch_id = self._authorize()
        self._bind(epoch_id)
        context = self._execute(
            RECORD_CONTEXT,
            {
                "context_id": "context:context.formal-selector",
                "subject": "Exact material for one formal verification.",
                "question": "Does the selected construction survive verification?",
                "material": [
                    {
                        "id": "mission:mission.1",
                        "why": "The current Mission fixes the exact research scope.",
                    }
                ],
            },
            executive_epoch_id=epoch_id,
        )
        self.assertEqual(context["status"], "completed", context)
        context_id = str(context["result"]["record_id"])

        first = self._execute(
            RECORD_STRATEGY,
            {
                "mission_continuation": "continue",
                "integrated_comparison": (
                    "The formal construction has the sharpest current discriminator."
                ),
                "selected_bets": [
                    {
                        "bet": "Continue ordinary source comparison.",
                        "discriminator": "The two normalizations agree or split.",
                    },
                    {
                        "bet": "Verify the first exact formal construction.",
                        "discriminator": "The exact parameter identity holds or fails.",
                        "formal_request": {
                            "purpose": "targeted_verification",
                            "context": {
                                "id": context_id,
                            },
                        },
                    },
                ],
                "reconsideration_conditions": [
                    {
                        "condition": (
                            "Reconsider after the exact parameter identity is checked."
                        )
                    }
                ],
            },
            executive_epoch_id=epoch_id,
        )
        self.assertEqual(first["status"], "completed", first)
        first_rows = first["result"]["formal_requests"]
        self.assertEqual(len(first_rows), 1)
        first_row = first_rows[0]
        self.assertEqual(
            set(first_row),
            {
                "selected_bet_sha256",
                "bet",
                "discriminator",
                "purpose",
                "context_retrieval_handle",
            },
        )
        self.assertEqual(first_row["purpose"], "targeted_verification")
        self.assertEqual(
            first_row["context_retrieval_handle"],
            context_id + "@1",
        )

        persisted_first = read_mission_strategy_head(
            self.store, mission_id="mission.1"
        )
        formal_bet = next(
            item
            for item in persisted_first.record.document["selected_bets"]
            if "formal_request" in item
        )
        expected_first_digest = hashlib.sha256(
            canonical_json_bytes(formal_bet)
        ).hexdigest()
        self.assertEqual(first_row["selected_bet_sha256"], expected_first_digest)
        self.assertTrue(
            all(
                "selected_bet_sha256" not in item
                for item in persisted_first.record.document["selected_bets"]
            )
        )

        authority = self.interface._direct_epoch_authority(epoch_id)
        self.assertIsNotNone(authority)
        assert authority is not None
        _mission_ref, resolved_strategy, selection, resolved_context = (
            _current_formal_request(
                self.store,
                authority=authority,
                selected_bet_sha256=str(first_row["selected_bet_sha256"]),
            )
        )
        self.assertEqual(resolved_strategy.to_reference(), persisted_first.to_reference())
        self.assertEqual(selection.selected_bet_sha256, expected_first_digest)
        self.assertEqual(
            resolved_context.record.document["context_id"],
            context_id.partition(":")[2],
        )

        oriented = self._execute(ORIENT, {}, executive_epoch_id=epoch_id)
        oriented_strategy = oriented["result"]["orientation"]["current_strategy"]
        self.assertEqual(
            oriented_strategy["formal_requests"],
            first_rows,
        )
        searched = self._execute(
            RETRIEVE,
            {
                "mode": "search",
                "purpose": "Find the exact first formal construction.",
                "query": "first exact formal construction",
            },
            executive_epoch_id=epoch_id,
        )
        searched_strategy = next(
            item for item in searched["result"]["items"] if item["kind"] == "strategy"
        )
        self.assertNotIn("readable_content", searched_strategy)
        selected_strategy = self._execute(
            RETRIEVE,
            {"mode": "read", "purpose": "Read the exact discovered formal selector.",
             "ids": [searched_strategy["id"]]},
            executive_epoch_id=epoch_id,
        )["result"]["items"][0]
        self.assertEqual(
            json.loads(selected_strategy["readable_content"])["formal_requests"],
            first_rows,
        )

        second = self._execute(
            RECORD_STRATEGY,
            {
                "mission_continuation": "continue",
                "integrated_comparison": (
                    "A corrected construction now owns the formal discriminator."
                ),
                "selected_bets": [
                    {
                        "bet": "Verify the corrected exact formal construction.",
                        "discriminator": (
                            "The corrected parameter identity holds or fails."
                        ),
                        "formal_request": {
                            "purpose": "targeted_verification",
                            "context": {
                                "id": context_id,
                            },
                        },
                    }
                ],
                "reconsideration_conditions": [
                    {
                        "condition": (
                            "Reconsider after the corrected identity is checked."
                        )
                    }
                ],
            },
            executive_epoch_id=epoch_id,
        )
        self.assertEqual(second["status"], "completed", second)
        second_rows = second["result"]["formal_requests"]
        self.assertEqual(len(second_rows), 1)
        self.assertNotEqual(
            second_rows[0]["selected_bet_sha256"],
            first_row["selected_bet_sha256"],
        )
        with self.assertRaisesRegex(StaleCommandError, "absent or changed"):
            _current_formal_request(
                self.store,
                authority=authority,
                selected_bet_sha256=str(first_row["selected_bet_sha256"]),
            )
        _current_formal_request(
            self.store,
            authority=authority,
            selected_bet_sha256=str(second_rows[0]["selected_bet_sha256"]),
        )

        exact_reads = self._execute(
            RETRIEVE,
            {
                "mode": "read",
                "purpose": "Compare historical and current formal selectors.",
                "ids": [
                    str(first["result"]["record_id"]) + "@2",
                    str(first["result"]["record_id"]),
                ],
            },
            executive_epoch_id=epoch_id,
        )
        readable_by_id = {
            item["id"]: json.loads(item["readable_content"])
            for item in exact_reads["result"]["items"]
        }
        self.assertEqual(
            readable_by_id[str(first["result"]["record_id"]) + "@2"][
                "formal_requests"
            ],
            first_rows,
        )
        self.assertEqual(
            readable_by_id[str(first["result"]["record_id"]) + "@3"][
                "formal_requests"
            ],
            second_rows,
        )

        reconstruction = self.interface.reconstruct()
        strategy_delta = next(
            item
            for item in reconstruction["recovery_opening"]["owner_deltas"]
            if item["current_reference"]["kind"] == "strategy"
        )
        self.assertEqual(strategy_delta["summary"]["formal_requests"], second_rows)
        selected = self.interface.reconstruct(
            selected_readback_handles=[strategy_delta["retrieval_handle"]]
        )["selected_readback"][0]
        self.assertTrue(
            all(
                "selected_bet_sha256" not in item
                for item in selected["document"]["selected_bets"]
            )
        )

    def test_open_formal_session_reconstructs_through_canonical_readback(
        self,
    ) -> None:
        epoch_id = self._authorize()
        self._bind(epoch_id)
        context = self._execute(
            RECORD_CONTEXT,
            {
                "context_id": "context:context.open-formal-session-recovery",
                "subject": "One exact construction awaiting formal verification.",
                "question": "Can the retained construction enter formal execution?",
                "material": [
                    {
                        "id": "mission:mission.1",
                        "why": "The Mission owns the exact verification scope.",
                    }
                ],
            },
            executive_epoch_id=epoch_id,
        )
        self.assertEqual(context["status"], "completed", context)
        strategy = self._execute(
            RECORD_STRATEGY,
            {
                "mission_continuation": "continue",
                "integrated_comparison": (
                    "The retained construction is ready for formal verification."
                ),
                "selected_bets": [
                    {
                        "bet": "Execute the retained formal verification.",
                        "discriminator": "The construction succeeds or fails exactly.",
                        "formal_request": {
                            "purpose": "targeted_verification",
                            "context": {"id": str(context["result"]["record_id"])},
                        },
                    }
                ],
                "reconsideration_conditions": [
                    {"condition": "Reconsider after the formal result is retained."}
                ],
            },
            executive_epoch_id=epoch_id,
        )
        self.assertEqual(strategy["status"], "completed", strategy)
        selector = str(strategy["result"]["formal_requests"][0]["selected_bet_sha256"])
        authority = self.interface._direct_epoch_authority(epoch_id)
        self.assertIsNotNone(authority)
        assert authority is not None
        formal_request = _current_formal_request(
            self.store,
            authority=authority,
            selected_bet_sha256=selector,
        )[2]
        prepared = prepare_formal_session_creation(
            self.store,
            authority=authority,
            formal_request=formal_request,
        )
        commit_formal_session_creation(
            self.store,
            authority=authority,
            prepared=prepared,
            lease=self.store.reissue_writer_lease(attest_current_principal()),
            actor="test.open-formal-session-recovery",
        )

        self._assert_recovered_formal_session(
            session_id=str(prepared.record.document["session_id"]),
            expected_lifecycle="open",
            expected_revision=1,
        )

    def test_record_strategy_selector_drives_terminal_attempt_then_stales(
        self,
    ) -> None:
        epoch_id = self._authorize()
        self._bind(epoch_id)
        context = self._execute(
            RECORD_CONTEXT,
            {
                "context_id": "context:context.formal-attempt-integration",
                "subject": "One exact construction for deterministic verification.",
                "question": "Does the construction pass the exact formal check?",
                "material": [
                    {
                        "id": "mission:mission.1",
                        "why": "The Mission owns the exact verification scope.",
                    }
                ],
            },
            executive_epoch_id=epoch_id,
        )
        self.assertEqual(context["status"], "completed", context)
        context_id = str(context["result"]["record_id"])
        first = self._execute(
            RECORD_STRATEGY,
            {
                "mission_continuation": "continue",
                "integrated_comparison": (
                    "The exact construction is ready for formal verification."
                ),
                "selected_bets": [
                    {
                        "bet": "Execute the exact formal verification.",
                        "discriminator": "The construction succeeds or fails exactly.",
                        "formal_request": {
                            "purpose": "targeted_verification",
                            "context": {"id": context_id},
                        },
                    }
                ],
                "reconsideration_conditions": [
                    {"condition": "Reconsider after the formal result is retained."}
                ],
            },
            executive_epoch_id=epoch_id,
        )
        self.assertEqual(first["status"], "completed", first)
        first_selector = str(
            first["result"]["formal_requests"][0]["selected_bet_sha256"]
        )

        runtime_root = Path(self.temporary.name) / "formal-attempt-runtime"
        release_commit = "b" * 40
        release_root = runtime_root / release_commit
        state_root = runtime_root / "state"
        codex_home = runtime_root / "codex-home"
        release_root.mkdir(parents=True)
        state_root.mkdir(parents=True)
        codex_home.mkdir(parents=True)
        (release_root / "source.txt").write_text(
            "immutable formal Attempt source\n",
            encoding="utf-8",
        )
        (release_root / ".mathematical-research-release.json").write_bytes(
            canonical_json_bytes(
                {
                    "schema_version": "wc.rh_mission_host_release.v1",
                    "release_sha": release_commit,
                    "bundle_sha256": "7" * 64,
                    "bundle_size": 4096,
                    "node_version": "v24.8.0",
                    "pnpm_version": "10.16.1",
                    "codex_version": "codex-cli 0.153.4",
                    "service_package": "@workstation-control/rh-mission-host",
                }
            )
        )
        context_root = state_root / "context-packages"
        context_root.mkdir()
        facts = FormalAttemptRuntimeFacts(
            release_root=release_root,
            release_commit=release_commit,
            state_root=state_root,
            codex_executable=Path(sys.executable).resolve(),
            codex_home=codex_home,
            outer_containment_id="rh-formal-integration-boundary.1",
            polling_cadence_seconds=0.125,
        )
        backend = FakeProviderBackend()
        provider = FakeAttemptProvider(backend, provider_id="codex_exec")
        adapter = ResearchAttemptAdapter(
            journal=AttemptJournal(state_root / "attempt-journal.sqlite3"),
            providers={provider.provider_id: provider},
            source_verifier=FakeSourceVerifier(),
            input_stage_store_root=state_root / "input-stages",
            scratch_store_root=state_root / "scratch",
            provider_output_root=state_root / "provider-output",
            artifact_store_root=state_root / "sealed-output",
            protected_root=codex_home,
            source_attachment_roots=(context_root,),
            outer_containment=facts.outer_containment,
        )

        def succeed(request: LaunchRequest) -> None:
            attempt_id = request.attempt_id
            output = Path(request.execution_spec.output_directory) / "formal-result.txt"
            output.write_text("The deterministic formal check succeeded.\n", encoding="utf-8")
            backend.observations[attempt_id] = ProviderObservation(
                provider_id="codex_exec",
                provider_ref=f"fake:{attempt_id}",
                state=ProviderState.SUCCEEDED,
                detail_code="fake_succeeded",
                exit_code=0,
                artifacts=(
                    OutputArtifact(
                        name="formal-result.txt",
                        path=str(output),
                        sha256=sha256_file(output),
                        size_bytes=output.stat().st_size,
                        media_type="text/plain",
                        encoding="utf-8",
                    ),
                ),
            )

        backend.on_launch = succeed
        authority = self.interface._direct_epoch_authority(epoch_id)
        self.assertIsNotNone(authority)
        assert authority is not None
        completed = execute_formal_attempt_operation(
            self.store,
            cas=self.cas,
            authority=authority,
            adapter=adapter,
            runtime=facts,
            selected_bet_sha256=first_selector,
            correction_basis=None,
            lease=self.store.reissue_writer_lease(attest_current_principal()),
            actor="test.formal-selector-integration",
            sleep=lambda _cadence: self.fail("terminal fake Attempt must not poll"),
        )
        self.assertEqual(completed.attempt_result.attempt_state, AttemptState.SUCCEEDED)
        self.assertTrue(completed.session_is_terminal)
        self.assertIsNotNone(completed.terminal_outcome)
        self.assertIsNotNone(completed.raw_capture)
        self.assertEqual(backend.launch_calls, 1)
        self.assertEqual(len(adapter.journal.list_attempts()), 1)
        self._assert_recovered_formal_session(
            session_id=completed.attempt_result.session_id,
            expected_lifecycle="terminal",
            expected_revision=2,
        )

        second = self._execute(
            RECORD_STRATEGY,
            {
                "mission_continuation": "continue",
                "integrated_comparison": (
                    "The retained result changes the exact next formal construction."
                ),
                "selected_bets": [
                    {
                        "bet": "Verify the successor formal construction.",
                        "discriminator": "The successor succeeds or fails exactly.",
                        "formal_request": {
                            "purpose": "targeted_verification",
                            "context": {"id": context_id},
                        },
                    }
                ],
                "reconsideration_conditions": [
                    {"condition": "Reconsider after the successor is checked."}
                ],
            },
            executive_epoch_id=epoch_id,
        )
        self.assertEqual(second["status"], "completed", second)
        self.assertGreater(int(second["result"]["revision"]), int(first["result"]["revision"]))
        self.assertEqual(
            read_mission_strategy_head(self.store, mission_id="mission.1").revision,
            second["result"]["revision"],
        )
        self.assertNotEqual(
            second["result"]["formal_requests"][0]["selected_bet_sha256"],
            first_selector,
        )
        launches_before_stale_call = backend.launch_calls
        reconciliations_before_stale_call = backend.reconcile_calls
        attempts_before_stale_call = len(adapter.journal.list_attempts())
        with self.assertRaisesRegex(StaleCommandError, "absent or changed"):
            execute_formal_attempt_operation(
                self.store,
                cas=self.cas,
                authority=authority,
                adapter=adapter,
                runtime=facts,
                selected_bet_sha256=first_selector,
                correction_basis=None,
                lease=self.store.reissue_writer_lease(attest_current_principal()),
                actor="test.formal-selector-integration",
                sleep=lambda _cadence: self.fail("a stale selector must not poll"),
            )
        self.assertEqual(backend.launch_calls, launches_before_stale_call)
        self.assertEqual(
            backend.reconcile_calls,
            reconciliations_before_stale_call,
        )
        self.assertEqual(
            len(adapter.journal.list_attempts()),
            attempts_before_stale_call,
        )

    def test_checkpoint_capture_failure_completes_and_emits_bounded_warning(
        self,
    ) -> None:
        epoch_id = self._authorize()
        self._bind(epoch_id)
        self._capture_pending_material(epoch_id)
        observer = CaptureWorkspaceObserver()
        self.interface = MissionInterface.open(
            self.store.paths.root,
            expected_project_id="project.rh",
            expected_mission_id="mission.1",
            observer=observer,
        )
        self.store = self.interface._store
        with mock.patch.object(
            self.store,
            "_publish_quiesced_checkpoint_source",
            side_effect=OSError(errno.EIO, "forced checkpoint source capture failure"),
        ):
            checkpointed = self._execute(
                CHECKPOINT,
                {},
                executive_epoch_id=epoch_id,
            )
        self.assertEqual(checkpointed["status"], "completed", checkpointed)
        self.assertNotIn("checkpoint_source", checkpointed)
        self.assertIsNotNone(
            self.store.read_continuation_checkpoint(executive_epoch_id=epoch_id)
        )
        metadata = self.store.read_metadata()
        self.assertEqual(metadata["lifecycle"], "active")
        self.assertIsNotNone(metadata["current_writer_epoch"])
        warning = [
            event
            for event in observer.events
            if event.kind is EventKind.BACKUP_CHANGED
            and event.attributes.get("status")
            == "checkpoint_source_capture_failed_writer_reacquired"
        ]
        self.assertEqual(len(warning), 1)
        self.assertEqual(
            warning[0].attributes["reason_code"],
            "checkpoint_source_capture_failed",
        )
        self.assertGreater(int(warning[0].attributes["byte_count"]), 0)
        self.assertGreaterEqual(
            float(warning[0].attributes["duration_seconds"]), 0.0
        )

    def test_checkpoint_source_integrity_failure_preserves_safe_writer_and_commit(
        self,
    ) -> None:
        epoch_id = self._authorize()
        self._bind(epoch_id)
        self._capture_pending_material(epoch_id)
        with mock.patch.object(
            self.store,
            "_publish_quiesced_checkpoint_source",
            side_effect=CheckpointSourceError(
                "checkpoint_source_path_unsafe",
                "forced unsafe checkpoint source path",
            ),
        ):
            with self.assertRaises(
                CheckpointCommittedSourceHandoffError
            ) as captured:
                self._execute(
                    CHECKPOINT,
                    {},
                    executive_epoch_id=epoch_id,
                )

        self.assertEqual(
            captured.exception.reason_code,
            "checkpoint_source_path_unsafe",
        )
        self.assertEqual(captured.exception.source_publication_state, "unknown")
        self.assertEqual(captured.exception.continuation_safety, "safe")
        self.assertIsNotNone(
            self.store.read_continuation_checkpoint(executive_epoch_id=epoch_id)
        )
        writer = self.store.reissue_writer_lease(attest_current_principal())
        self.assertEqual(
            self.store.read_metadata()["current_writer_epoch"],
            writer.epoch,
        )

    def test_checkpoint_lock_contention_preserves_saved_checkpoint_and_writer(
        self,
    ) -> None:
        predecessor_epoch = self._authorize()
        self._bind(predecessor_epoch)
        self._capture_pending_material(predecessor_epoch)
        predecessor = self._execute(
            CHECKPOINT,
            {},
            executive_epoch_id=predecessor_epoch,
        )
        self.assertEqual(predecessor["status"], "completed", predecessor)

        prior_preflight = self.store.inspect_checkpoint_source_preflight(
            mission_id="mission.1",
            pending_claim_id=None,
        )
        prior_projection = prior_preflight["unclaimed_source"]
        self.assertIsNotNone(prior_projection)
        assert prior_projection is not None
        prior_source_path = self.store._checkpoint_source_path(claimed=False)
        prior_source_bytes = prior_source_path.read_bytes()

        observer = CaptureWorkspaceObserver()
        self.interface = MissionInterface.open(
            self.store.paths.root,
            expected_project_id="project.rh",
            expected_mission_id="mission.1",
            observer=observer,
        )
        self.store = self.interface._store
        self.cas = EvidenceCAS(self.store.paths)
        self.goal_thread_id = "goal-thread:checkpoint-contention-successor"
        self.goal_workspace = self.goal_workspace.parent / "contention-successor"
        self.goal_workspace.mkdir()
        successor_epoch = self._authorize()
        self._bind(successor_epoch)
        recorded = self._execute(
            RECORD_CONTEXT,
            {
                "context_id": "context:checkpoint-contention-work",
                "subject": "Checkpoint contention regression",
                "question": "Does a competing backup lock preserve the checkpoint?",
                "material": [
                    {
                        "id": "mission:mission.1",
                        "why": "The Mission fixes the checkpoint handoff scope.",
                    }
                ],
            },
            executive_epoch_id=successor_epoch,
        )
        self.assertEqual(recorded["status"], "completed", recorded)
        strategy_before = read_mission_strategy_head(
            self.store,
            mission_id="mission.1",
        )
        metadata_before = self.store.read_metadata()
        writer_before = self.store.reissue_writer_lease(attest_current_principal())
        contender = WorkspaceStore.open(
            self.store.paths,
            expected_project_id="project.rh",
        )

        operation_acquired = threading.Event()
        release_operation = threading.Event()
        inspection_errors: list[BaseException] = []
        inspect_source_file = contender._checkpoint_source_from_file

        def hold_public_inspection(*args, **kwargs):
            operation_acquired.set()
            if not release_operation.wait(timeout=10):
                raise AssertionError("timed out holding checkpoint-source inspection")
            return inspect_source_file(*args, **kwargs)

        def inspect_source() -> None:
            try:
                contender.inspect_checkpoint_source_preflight(
                    mission_id="mission.1",
                    pending_claim_id=None,
                )
            except BaseException as exc:  # surfaced on the test thread below
                inspection_errors.append(exc)

        with mock.patch.object(
            contender,
            "_checkpoint_source_from_file",
            side_effect=hold_public_inspection,
        ):
            inspection_thread = threading.Thread(
                target=inspect_source,
                name="checkpoint-source-public-inspection",
            )
            inspection_thread.start()
            try:
                self.assertTrue(
                    operation_acquired.wait(timeout=10),
                    "public source inspection did not acquire its operation lease",
                )
                checkpointed = self._execute(
                    CHECKPOINT,
                    {},
                    executive_epoch_id=successor_epoch,
                )
            finally:
                release_operation.set()
                inspection_thread.join(timeout=10)
        self.assertFalse(
            inspection_thread.is_alive(),
            "public source inspection did not release its operation lease",
        )
        self.assertEqual(inspection_errors, [])

        self.assertEqual(checkpointed["status"], "completed", checkpointed)
        self.assertEqual(checkpointed["operation"], CHECKPOINT)
        self.assertEqual(checkpointed["result"]["state"], "checkpointed")
        self.assertIsNotNone(
            self.store.read_continuation_checkpoint(
                executive_epoch_id=successor_epoch
            )
        )
        self.assertEqual(self.interface.current_epoch()["state"], "closed")
        metadata_after = self.store.read_metadata()
        self.assertEqual(
            metadata_after["current_writer_epoch"],
            metadata_before["current_writer_epoch"],
        )
        writer_after = self.store.reissue_writer_lease(attest_current_principal())
        self.assertEqual(writer_after.epoch, writer_before.epoch)
        self.assertEqual(writer_after.owner, writer_before.owner)
        strategy_after = read_mission_strategy_head(
            self.store,
            mission_id="mission.1",
        )
        self.assertEqual(strategy_after.revision, strategy_before.revision)
        self.assertEqual(
            strategy_after.record.payload_sha256,
            strategy_before.record.payload_sha256,
        )
        self.assertEqual(prior_source_path.read_bytes(), prior_source_bytes)
        after_preflight = self.store.inspect_checkpoint_source_preflight(
            mission_id="mission.1",
            pending_claim_id=None,
        )
        self.assertEqual(
            after_preflight["latest_checkpoint"]["checkpoint_id"],
            checkpointed["result"]["checkpoint_id"],
        )
        self.assertEqual(after_preflight["unclaimed_source"], prior_projection)
        warnings = [
            event
            for event in observer.events
            if event.kind is EventKind.BACKUP_CHANGED
            and event.attributes.get("status")
            == "checkpoint_source_snapshot_skipped_writer_preserved"
        ]
        self.assertEqual(len(warnings), 1)
        self.assertEqual(
            warnings[0].attributes["reason_code"],
            "checkpoint_source_busy",
        )
        self.assertFalse(
            any(
                event.kind is EventKind.BACKUP_CHANGED
                and event.attributes.get("status")
                == "checkpoint_source_published"
                for event in observer.events
            )
        )
        replayed = self._execute(
            CHECKPOINT,
            {},
            executive_epoch_id=successor_epoch,
        )
        self.assertEqual(replayed, checkpointed)
        self.assertEqual(
            len(
                [
                    event
                    for event in observer.events
                    if event.kind is EventKind.BACKUP_CHANGED
                    and event.attributes.get("status")
                    == "checkpoint_source_snapshot_skipped_writer_preserved"
                ]
            ),
            1,
        )

    def test_checkpoint_operation_teardown_failure_revalidates_and_succeeds(
        self,
    ) -> None:
        class FailingExitLease:
            def __enter__(self):
                return object()

            def __exit__(self, *_args):
                raise OSError("forced checkpoint-source lease teardown failure")

        epoch_id = self._authorize()
        self._bind(epoch_id)
        self._capture_pending_material(epoch_id)
        observer = CaptureWorkspaceObserver()
        self.interface = MissionInterface.open(
            self.store.paths.root,
            expected_project_id="project.rh",
            expected_mission_id="mission.1",
            observer=observer,
        )
        self.store = self.interface._store
        with mock.patch(
            "research_core.workspace_store.acquire_kernel_lease",
            return_value=FailingExitLease(),
        ):
            checkpointed = self._execute(
                CHECKPOINT,
                {},
                executive_epoch_id=epoch_id,
            )

        self.assertEqual(checkpointed["status"], "completed")
        self.assertEqual(checkpointed["result"]["state"], "checkpointed")
        self.assertIsNotNone(
            self.store.read_continuation_checkpoint(executive_epoch_id=epoch_id)
        )
        writer = self.store.reissue_writer_lease(attest_current_principal())
        warnings = [
            event
            for event in observer.events
            if event.kind is EventKind.BACKUP_CHANGED
            and event.attributes.get("status")
            == "checkpoint_source_operation_teardown_failed_writer_revalidated"
        ]
        self.assertEqual(len(warnings), 1)
        self.assertEqual(
            warnings[0].attributes["reason_code"],
            "checkpoint_source_published",
        )
        self.assertFalse(
            any(
                event.kind is EventKind.BACKUP_CHANGED
                and event.attributes.get("status") == "checkpoint_source_published"
                for event in observer.events
            )
        )
        self.assertEqual(
            self.store.read_metadata()["current_writer_epoch"],
            writer.epoch,
        )

    def test_checkpoint_writer_reacquisition_failure_propagates_fatally(
        self,
    ) -> None:
        epoch_id = self._authorize()
        self._bind(epoch_id)
        self._capture_pending_material(epoch_id)
        with mock.patch.object(
            self.store,
            "_claim_writer_without_checkpoint_source_lock",
            side_effect=StaleWriterError("forced successor writer fault"),
        ):
            with self.assertRaises(
                CheckpointCommittedSourceHandoffError
            ) as captured:
                self._execute(
                    CHECKPOINT,
                    {},
                    executive_epoch_id=epoch_id,
                )
        self.assertIsInstance(
            captured.exception.original_error,
            CheckpointSourceReacquisitionError,
        )
        self.assertEqual(
            captured.exception.checkpoint["executive_epoch_id"],
            epoch_id,
        )
        self.assertEqual(captured.exception.source_publication_state, "published")
        self.assertEqual(captured.exception.continuation_safety, "unsafe")
        self.assertIsNotNone(
            self.store.read_continuation_checkpoint(executive_epoch_id=epoch_id)
        )
        metadata = self.store.read_metadata()
        self.assertEqual(metadata["lifecycle"], "quiesced")
        self.assertIsNone(metadata["current_writer_epoch"])
        with self.assertRaises(
            CheckpointCommittedSourceHandoffError
        ) as replayed:
            self._execute(
                CHECKPOINT,
                {},
                executive_epoch_id=epoch_id,
            )
        self.assertEqual(replayed.exception.checkpoint, captured.exception.checkpoint)
        self.assertEqual(
            replayed.exception.reason_code,
            "checkpoint_source_writer_unavailable",
        )
        self.assertEqual(replayed.exception.source_publication_state, "unknown")
        self.assertEqual(replayed.exception.continuation_safety, "unsafe")

    def test_checkpoint_committed_release_state_unverified_preserves_result(
        self,
    ) -> None:
        epoch_id = self._authorize()
        self._bind(epoch_id)
        self._capture_pending_material(epoch_id)
        release_writer = self.store.release_writer

        def release_then_fail(*args, **kwargs):
            release_writer(*args, **kwargs)
            raise RuntimeError("forced ambiguous release response")

        with mock.patch.object(
            self.store,
            "release_writer",
            side_effect=release_then_fail,
        ):
            with self.assertRaises(
                CheckpointCommittedSourceHandoffError
            ) as captured:
                self._execute(
                    CHECKPOINT,
                    {},
                    executive_epoch_id=epoch_id,
                )
        self.assertEqual(
            captured.exception.reason_code,
            "checkpoint_source_writer_state_unverified",
        )
        self.assertEqual(captured.exception.source_publication_state, "unknown")
        self.assertEqual(captured.exception.continuation_safety, "unverified")
        self.assertIsNotNone(
            self.store.read_continuation_checkpoint(executive_epoch_id=epoch_id)
        )
        metadata = self.store.read_metadata()
        self.assertEqual(metadata["lifecycle"], "quiesced")
        self.assertIsNone(metadata["current_writer_epoch"])

    def test_checkpoint_write_scope_exit_failure_preserves_committed_result(
        self,
    ) -> None:
        epoch_id = self._authorize()
        self._bind(epoch_id)
        self._capture_pending_material(epoch_id)
        checkpoint_scope = self.store.direct_checkpoint_write_scope

        @contextmanager
        def fail_after_commit():
            with checkpoint_scope():
                yield
            raise OSError("forced checkpoint write-scope exit failure")

        with mock.patch.object(
            self.store,
            "direct_checkpoint_write_scope",
            side_effect=fail_after_commit,
        ):
            with self.assertRaises(
                CheckpointCommittedSourceHandoffError
            ) as captured:
                self._execute(
                    CHECKPOINT,
                    {},
                    executive_epoch_id=epoch_id,
                )
        self.assertEqual(captured.exception.source_publication_state, "unknown")
        self.assertEqual(captured.exception.continuation_safety, "unverified")
        retained = self.store.read_continuation_checkpoint(
            executive_epoch_id=epoch_id
        )
        self.assertIsNotNone(retained)
        assert retained is not None
        self.assertEqual(
            captured.exception.checkpoint["checkpoint_id"],
            retained["checkpoint_id"],
        )
        self.assertEqual(self.interface.current_epoch()["state"], "closed")
        self.store.reissue_writer_lease(attest_current_principal())

    def test_checkpoint_postcommit_ack_failure_continues_source_handoff(
        self,
    ) -> None:
        epoch_id = self._authorize()
        self._bind(epoch_id)
        self._capture_pending_material(epoch_id)
        strategy_before = read_mission_strategy_head(
            self.store,
            mission_id="mission.1",
        )

        with mock.patch.object(
            self.store,
            "_after_commit",
            side_effect=RuntimeError("forced checkpoint acknowledgement failure"),
        ) as after_commit:
            checkpointed = self._execute(
                CHECKPOINT,
                {},
                executive_epoch_id=epoch_id,
            )

        self.assertEqual(checkpointed["status"], "completed")
        self.assertEqual(checkpointed["result"]["state"], "checkpointed")
        after_commit.assert_called_once()
        retained = self.store.read_continuation_checkpoint(
            executive_epoch_id=epoch_id
        )
        self.assertIsNotNone(retained)
        assert retained is not None
        self.assertEqual(
            checkpointed["result"]["checkpoint_id"],
            retained["checkpoint_id"],
        )
        self.assertEqual(self.interface.current_epoch()["state"], "closed")
        writer = self.store.reissue_writer_lease(attest_current_principal())
        self.assertEqual(
            self.store.read_metadata()["current_writer_epoch"],
            writer.epoch,
        )
        latest = self.store.inspect_checkpoint_source_preflight(
            mission_id="mission.1",
            pending_claim_id=None,
        )
        self.assertIsNotNone(latest["unclaimed_source"])
        self.assertEqual(
            latest["unclaimed_source"]["checkpoint_id"],
            retained["checkpoint_id"],
        )
        strategy_after = read_mission_strategy_head(
            self.store,
            mission_id="mission.1",
        )
        self.assertEqual(strategy_after.revision, strategy_before.revision)
        self.assertEqual(
            strategy_after.record.payload_sha256,
            strategy_before.record.payload_sha256,
        )

    def test_checkpoint_ack_identity_drift_replays_exact_commit_before_handoff(
        self,
    ) -> None:
        epoch_id = self._authorize()
        self._bind(epoch_id)
        self._capture_pending_material(epoch_id)
        commit_checkpoint = self.store.commit_direct_continuation_checkpoint
        commit_calls = 0

        def commit_then_misreport(*args, **kwargs):
            nonlocal commit_calls
            outcome = commit_checkpoint(*args, **kwargs)
            commit_calls += 1
            if commit_calls == 1:
                raise CommittedCommandAcknowledgementError(
                    "forced mismatched checkpoint acknowledgement",
                    outcome=replace(outcome, command_id="semantic-checkpoint:wrong"),
                    cause=RuntimeError("forced mismatched checkpoint acknowledgement"),
                )
            return outcome

        with mock.patch.object(
            self.store,
            "commit_direct_continuation_checkpoint",
            side_effect=commit_then_misreport,
        ):
            checkpointed = self._execute(
                CHECKPOINT,
                {},
                executive_epoch_id=epoch_id,
            )

        self.assertEqual(commit_calls, 2)
        self.assertEqual(checkpointed["status"], "completed")
        retained = self.store.read_continuation_checkpoint(
            executive_epoch_id=epoch_id
        )
        self.assertIsNotNone(retained)
        assert retained is not None
        self.assertEqual(
            checkpointed["result"]["checkpoint_id"],
            retained["checkpoint_id"],
        )
        source = self.store.inspect_checkpoint_source_preflight(
            mission_id="mission.1",
            pending_claim_id=None,
        )["unclaimed_source"]
        self.assertIsNotNone(source)
        self.assertEqual(source["checkpoint_id"], retained["checkpoint_id"])

    def test_checkpoint_ack_identity_drift_does_not_invent_result_when_replay_fails(
        self,
    ) -> None:
        epoch_id = self._authorize()
        self._bind(epoch_id)
        self._capture_pending_material(epoch_id)
        commit_checkpoint = self.store.commit_direct_continuation_checkpoint
        commit_calls = 0

        def commit_then_lose_exact_replay(*args, **kwargs):
            nonlocal commit_calls
            commit_calls += 1
            if commit_calls == 1:
                outcome = commit_checkpoint(*args, **kwargs)
                raise CommittedCommandAcknowledgementError(
                    "forced mismatched checkpoint acknowledgement",
                    outcome=replace(outcome, command_id="semantic-checkpoint:wrong"),
                    cause=RuntimeError("forced mismatched checkpoint acknowledgement"),
                )
            raise RuntimeError("forced exact replay failure")

        with mock.patch.object(
            self.store,
            "commit_direct_continuation_checkpoint",
            side_effect=commit_then_lose_exact_replay,
        ):
            with self.assertRaises(WorkspaceStoreError) as raised:
                self._execute(
                    CHECKPOINT,
                    {},
                    executive_epoch_id=epoch_id,
                )

        self.assertEqual(commit_calls, 2)
        self.assertEqual(
            getattr(raised.exception, "code", None),
            "checkpoint_commit_acknowledgement_mismatch",
        )
        retained = self.store.read_continuation_checkpoint(
            executive_epoch_id=epoch_id
        )
        self.assertIsNotNone(retained)
        assert retained is not None
        self.assertEqual(retained["executive_epoch_id"], epoch_id)
        self.assertIsNone(
            self.store.inspect_checkpoint_source_preflight(
                mission_id="mission.1",
                pending_claim_id=None,
            )["unclaimed_source"]
        )
        self.assertEqual(self.interface.current_epoch()["state"], "closed")
        writer = self.store.reissue_writer_lease(attest_current_principal())
        self.store.release_writer(
            writer,
            expected_project_commit=int(
                self.store.read_metadata()["current_project_commit"]
            ),
            expected_root_digest=str(
                self.store.read_metadata()["current_root_digest"]
            ),
            expected_canonical_authority_digest=str(
                self.store.read_metadata()["canonical_authority_digest"]
            ),
        )

    def test_checkpoint_successor_ack_failure_reissues_and_completes(
        self,
    ) -> None:
        epoch_id = self._authorize()
        self._bind(epoch_id)
        self._capture_pending_material(epoch_id)
        claim_writer = self.store._claim_writer_without_checkpoint_source_lock

        def claim_then_fail(*args, **kwargs):
            claim_writer(*args, **kwargs)
            raise RuntimeError("forced successor acknowledgement failure")

        with mock.patch.object(
            self.store,
            "_claim_writer_without_checkpoint_source_lock",
            side_effect=claim_then_fail,
        ):
            checkpointed = self._execute(
                CHECKPOINT,
                {},
                executive_epoch_id=epoch_id,
            )

        self.assertEqual(checkpointed["status"], "completed")
        self.assertEqual(checkpointed["result"]["state"], "checkpointed")
        retained = self.store.read_continuation_checkpoint(
            executive_epoch_id=epoch_id
        )
        self.assertIsNotNone(retained)
        assert retained is not None
        self.assertEqual(
            checkpointed["result"]["checkpoint_id"],
            retained["checkpoint_id"],
        )
        writer = self.store.reissue_writer_lease(attest_current_principal())
        self.assertGreater(writer.epoch, 1)

    def test_checkpoint_replay_summary_failure_preserves_committed_result(
        self,
    ) -> None:
        epoch_id = self._authorize()
        self._bind(epoch_id)
        self._capture_pending_material(epoch_id)
        checkpointed = self._execute(
            CHECKPOINT,
            {},
            executive_epoch_id=epoch_id,
        )
        with mock.patch.object(
            self.store,
            "read_current_mission_checkpoint_summary",
            side_effect=WorkspaceIntegrityError(
                "forced current checkpoint classification failure"
            ),
        ):
            with self.assertRaises(
                CheckpointCommittedSourceHandoffError
            ) as captured:
                self._execute(
                    CHECKPOINT,
                    {},
                    executive_epoch_id=epoch_id,
                )
        self.assertEqual(captured.exception.checkpoint, checkpointed["result"])
        self.assertEqual(captured.exception.source_publication_state, "unknown")
        self.assertEqual(captured.exception.continuation_safety, "unverified")

    def test_direct_epoch_orients_checkpoints_replays_and_fences_semantics(self) -> None:
        epoch_id = self._authorize()
        orient_request = self._request(ORIENT, {})

        with self.assertRaisesRegex(TypeError, "executive_epoch_id"):
            self.interface.execute_semantic_operation(orient_request)  # type: ignore[call-arg]

        before_binding = self.interface.execute_semantic_operation(
            orient_request,
            executive_epoch_id=epoch_id,
        )
        self.assertEqual(before_binding["status"], "rejected", before_binding)

        with self.assertRaises(MissionInterfaceError):
            self.interface.bind_executive_epoch_from_owner(
                {
                    "executiveEpochId": epoch_id,
                    "rootThreadId": self.goal_thread_id,
                    "workspaceRoot": str(self.store.paths.root),
                }
            )

        self._bind(epoch_id)
        oriented = self.interface.execute_semantic_operation(
            orient_request,
            executive_epoch_id=epoch_id,
        )
        self.assertEqual(oriented["status"], "completed", oriented)
        orientation = oriented["result"]["orientation"]
        self.assertTrue(orientation["current_strategy"]["handle"].startswith("strategy:"))
        self.assertEqual(orientation["schema_version"], "mathematical_research.executive_orientation.v3")
        self.assertNotIn("strategy_ground", orientation)
        self.assertNotIn("opportunity_portfolio", orientation)

        pending_capture_id = self._capture_pending_material(epoch_id)
        checkpointed = self.interface.execute_semantic_operation(
            self._request(CHECKPOINT, {}),
            executive_epoch_id=epoch_id,
        )
        self.assertEqual(checkpointed["status"], "completed", checkpointed)
        checkpoint_id = checkpointed["result"]["checkpoint_id"]
        chain = self.store.read_executive_epoch_events(
            executive_epoch_id=epoch_id,
            mission_id="mission.1",
        )
        checkpoint = self.store.read_continuation_checkpoint(
            executive_epoch_id=epoch_id
        )
        self.assertIsNotNone(checkpoint)
        assert checkpoint is not None
        self.assertEqual(
            tuple(item["event_kind"] for item in chain),
            ("authorized", "bound", "checkpointed"),
        )
        self.assertEqual(checkpoint["checkpoint_id"], checkpoint_id)
        self.assertEqual(checkpoint["document"]["schema_version"], 2)
        self.assertNotIn("pending_capture_locators", checkpoint["document"])
        checkpoint_sections = self._checkpoint_sections(checkpoint)
        self.assertEqual(
            checkpoint_sections["pending_capture_locators"],
            [
                {
                    "capture_id": pending_capture_id,
                    "artifact_ordinal": 0,
                }
            ],
        )
        self.assertEqual(
            checkpoint["project_commit_no"], chain[-1]["project_commit_no"]
        )
        self.assertEqual(self.interface.current_epoch()["state"], "closed")

        reconstruction = self.interface.reconstruct()
        self.assertEqual(
            set(reconstruction),
            {
                "schema_version",
                "project_id",
                "mission_id",
                "observed_project_commit",
                "recovery_opening",
                "latest_executive_epoch",
            },
        )
        self.assertEqual(
            reconstruction["schema_version"],
            "mathematical_research.direct_mission_reconstruction.v4",
        )
        opening = reconstruction["recovery_opening"]
        self.assertEqual(
            set(opening),
            {
                "cut",
                "owner_deltas",
                "open_candidate_a1",
                "hooks",
                "opportunity_portfolio",
                "raw_captures",
                "post_cut_transitions",
                "admitted_result",
            },
        )
        self.assertEqual(opening["open_candidate_a1"], [])
        self.assertIsNone(opening["admitted_result"])
        self.assertEqual(
            opening["cut"]["reference"]["checkpoint_id"],
            checkpoint_id,
        )
        self.assertEqual(opening["cut"]["document"], checkpoint["document"])
        deltas = {
            item["current_reference"]["kind"]: item
            for item in opening["owner_deltas"]
        }
        self.assertEqual(deltas["strategy"]["relation"], "unchanged")
        self.assertEqual(
            deltas["strategy"]["summary"]["mission_continuation"],
            "continue",
        )
        self.assertEqual(
            set(opening["hooks"]),
            {
                "unresolved",
                "revival",
                "recombination",
                "reconsideration",
                "reversal",
            },
        )
        pending_capture = next(
            item
            for item in opening["raw_captures"]
            if item["capture_id"] == pending_capture_id
        )
        self.assertEqual(
            pending_capture["late_classification"],
            "at_or_before_terminal",
        )
        self.assertTrue(pending_capture["artifacts"][0]["pending_at_cut"])
        self.assertTrue(pending_capture["artifacts"][0]["pending_current"])
        self.assertEqual(
            reconstruction["latest_executive_epoch"]["state"],
            "checkpointed",
        )
        self.assertEqual(
            set(reconstruction["latest_executive_epoch"]),
            {
                "executive_epoch_id",
                "state",
                "goal_thread_id",
                "last_event_project_commit",
                "checkpoint_ref",
                "reconciliation",
            },
        )
        self.assertEqual(
            reconstruction["latest_executive_epoch"]["checkpoint_ref"],
            opening["cut"]["reference"],
        )
        self.assertIsNone(
            reconstruction["latest_executive_epoch"]["reconciliation"]
        )
        self._assert_compact_recovery_projection(reconstruction)

        replay = self.interface.execute_semantic_operation(
            self._request(CHECKPOINT, {}),
            executive_epoch_id=epoch_id,
        )
        self.assertEqual(replay, checkpointed)
        self.assertEqual(
            len(
                self.store.read_executive_epoch_events(
                    executive_epoch_id=epoch_id,
                    mission_id="mission.1",
                )
            ),
            3,
        )

        later_write = self.interface.execute_semantic_operation(
            self._request(
                RECORD_CONTEXT,
                {
                    "context_id": "context:after-checkpoint",
                    "subject": "This semantic write must remain outside the closed epoch.",
                    "question": "Can a closed epoch still revise semantic owners?",
                    "material": [
                        {
                            "id": "mission:mission.1",
                            "why": "The current Mission is the exact lifecycle boundary.",
                        }
                    ],
                },
            ),
            executive_epoch_id=epoch_id,
        )
        self.assertEqual(later_write["status"], "rejected", later_write)
        self.assertIsNotNone(later_write["error"])

        late_observation = {
            "observationId": "observation.late-empty-after-checkpoint",
            "materialKind": "output",
            "content": "",
            "rootThreadId": self.goal_thread_id,
            "parentThreadId": self.goal_thread_id,
            "childThreadId": "child-thread:late-empty-output",
        }
        with mock.patch.object(
            self.interface,
            "_direct_epoch_authority",
            side_effect=AssertionError(
                "late raw capture must use historical Goal provenance"
            ),
        ):
            captured = self.interface.capture_native_material_observation(
                late_observation,
                executive_epoch_id=epoch_id,
            )
            replay = self.interface.capture_native_material_observation(
                late_observation,
                executive_epoch_id=epoch_id,
            )
        self.assertEqual(replay, captured)
        self.assertEqual(captured["custody_status"], "store_cas_verified")
        capture_id = str(captured["material_id"]).partition(":")[2]
        stored_capture = self.store.read_raw_capture(capture_id)
        self.assertEqual(
            stored_capture["record"]["executive_epoch_id"],
            epoch_id,
        )
        self.assertEqual(
            stored_capture["record"]["assignment_id"],
            "child-thread:late-empty-output",
        )
        self.assertEqual(
            stored_capture["record"]["provenance"]["parent_thread_id"],
            self.goal_thread_id,
        )
        self.assertEqual(
            stored_capture["artifacts"][0]["blob_sha256"],
            hashlib.sha256(b"").hexdigest(),
        )

    def test_checkpoint_replay_preserves_an_older_exact_cut_after_a_successor_cut(
        self,
    ) -> None:
        first_epoch = self._authorize()
        self._bind(first_epoch)
        self._capture_pending_material(first_epoch)
        first = self.interface.execute_semantic_operation(
            self._request(CHECKPOINT, {}),
            executive_epoch_id=first_epoch,
        )
        self.assertEqual(first["status"], "completed", first)

        self.goal_thread_id = "goal-thread:checkpoint-replay-successor"
        self.goal_workspace = (
            Path(self.temporary.name) / "goal-workspaces" / "successor"
        )
        self.goal_workspace.mkdir(parents=True)
        successor_epoch = self._authorize()
        self._bind(successor_epoch)
        advanced = self._execute(
            RECORD_STRATEGY,
            {
                "mission_continuation": "continue",
                "integrated_comparison": (
                    "The successor retains a distinct exact checkpoint cut."
                ),
                "reconsideration_conditions": [
                    {
                        "condition": (
                            "Reconsider after exact new mathematical evidence."
                        )
                    }
                ],
            },
            executive_epoch_id=successor_epoch,
        )
        self.assertEqual(advanced["status"], "completed", advanced)
        successor = self.interface.execute_semantic_operation(
            self._request(CHECKPOINT, {}),
            executive_epoch_id=successor_epoch,
        )
        self.assertEqual(successor["status"], "completed", successor)
        self.assertNotEqual(
            successor["result"]["checkpoint_id"],
            first["result"]["checkpoint_id"],
        )

        replay = self.interface.execute_semantic_operation(
            self._request(CHECKPOINT, {}),
            executive_epoch_id=first_epoch,
        )
        self.assertEqual(replay, first)
        self.assertEqual(
            tuple(
                item["event_kind"]
                for item in self.store.read_executive_epoch_events(
                    executive_epoch_id=first_epoch,
                    mission_id="mission.1",
                )
            ),
            ("authorized", "bound", "checkpointed"),
        )

    def test_native_helper_lineage_survives_capture_retrieval_and_recovery(
        self,
    ) -> None:
        epoch_id = self._authorize()
        self._bind(epoch_id)
        researcher_thread_id = "thread.researcher.lineage"
        helper_thread_id = "thread.researcher.lineage.helper"

        captured = self.interface.capture_native_material_observation(
            {
                "observationId": "observation.helper.lineage",
                "materialKind": "output",
                "content": "The helper isolates one unique obstruction.",
                "rootThreadId": self.goal_thread_id,
                "parentThreadId": researcher_thread_id,
                "childThreadId": helper_thread_id,
            },
            executive_epoch_id=epoch_id,
        )
        capture_id = str(captured["material_id"]).partition(":")[2]
        lineage = {
            "parent_thread_id": researcher_thread_id,
            "child_thread_id": helper_thread_id,
        }
        stored = self.store.read_raw_capture(capture_id)
        self.assertEqual(
            stored["record"]["provenance"]["parent_thread_id"],
            researcher_thread_id,
        )
        self.assertEqual(
            stored["record"]["provenance"]["child_thread_id"],
            helper_thread_id,
        )

        searched = self._execute(
            RETRIEVE,
            {
                "mode": "search",
                "purpose": "Recover the helper output through its direct researcher.",
                "query": researcher_thread_id,
            },
            executive_epoch_id=epoch_id,
        )
        self.assertEqual(searched["status"], "completed", searched)
        capture_items = [
            item
            for item in searched["result"]["items"]
            if item["id"] == f"capture:{capture_id}"
        ]
        self.assertEqual(len(capture_items), 1, capture_items)
        self.assertEqual(
            capture_items[0]["native_lineage"],
            {**lineage, "root_thread_id": self.goal_thread_id},
        )

        exact = self._execute(
            RETRIEVE,
            {
                "mode": "read",
                "purpose": "Read the exact helper capture with its branch lineage.",
                "ids": [f"capture:{capture_id}"],
            },
            executive_epoch_id=epoch_id,
        )
        self.assertEqual(exact["status"], "completed", exact)
        self.assertEqual(
            json.loads(exact["result"]["items"][0]["readable_content"])[
                "native_lineage"
            ],
            lineage,
        )

        reconstruction = self.interface.reconstruct()
        descriptor = next(
            item
            for item in reconstruction["recovery_opening"]["raw_captures"]
            if item["capture_id"] == capture_id
        )
        self.assertEqual(descriptor["native_lineage"], lineage)

    def test_native_results_resolve_by_child_id_into_grouped_causal_learning(
        self,
    ) -> None:
        epoch_id = self._authorize()
        self._bind(epoch_id)

        context = self._execute(
            RECORD_CONTEXT,
            {
                "context_id": "context:context.shared-native-question",
                "subject": "Shared delegated discriminator",
                "question": (
                    "Do two independent constructions expose the same obstruction?"
                ),
                "material": [
                    {
                        "id": "mission:mission.1",
                        "why": (
                            "The current Mission supplies the relied-on objective and "
                            "proof standard for the shared question."
                        ),
                    }
                ],
            },
            executive_epoch_id=epoch_id,
        )
        self.assertEqual(context["status"], "completed", context)

        def capture(
            *, observation_id: str, child_thread_id: str, content: str
        ) -> dict[str, object]:
            return dict(
                self.interface.capture_native_material_observation(
                    {
                        "observationId": observation_id,
                        "materialKind": "output",
                        "content": content,
                        "rootThreadId": self.goal_thread_id,
                        "parentThreadId": self.goal_thread_id,
                        "childThreadId": child_thread_id,
                    },
                    executive_epoch_id=epoch_id,
                )
            )

        first = capture(
            observation_id="observation.shared-native.first",
            child_thread_id="child-thread:shared-native-first",
            content="Construction A isolates the same finite obstruction.",
        )
        second = capture(
            observation_id="observation.shared-native.second",
            child_thread_id="child-thread:shared-native-second",
            content="Construction B independently isolates that obstruction.",
        )
        unrelied = capture(
            observation_id="observation.shared-native.unrelied",
            child_thread_id="child-thread:shared-native-unrelied",
            content="An unrelated exploratory direction remains unselected.",
        )

        def resolve_capture(child_thread_id: str) -> str:
            searched = self._execute(
                RETRIEVE,
                {
                    "mode": "search",
                    "purpose": (
                        "Resolve the returned native result to its exact capture."
                    ),
                    "query": child_thread_id,
                },
                executive_epoch_id=epoch_id,
            )
            self.assertEqual(searched["status"], "completed", searched)
            capture_items = [
                item
                for item in searched["result"]["items"]
                if item["kind"] == "capture"
            ]
            self.assertEqual(len(capture_items), 1, capture_items)
            return str(capture_items[0]["id"])

        first_capture_id = resolve_capture("child-thread:shared-native-first")
        second_capture_id = resolve_capture("child-thread:shared-native-second")
        self.assertEqual(first_capture_id, first["material_id"])
        self.assertEqual(second_capture_id, second["material_id"])

        evidence = self._execute(
            INTERPRET_MATERIAL,
            {
                "evidence_id": "evidence:evidence.shared-native-obstruction",
                "capture_scopes": [
                    {
                        "captured_material_id": first_capture_id,
                        "artifact_ordinal": 0,
                        "exact_scope": "The complete first returned worker output.",
                        "coverage": "complete_artifact",
                    },
                    {
                        "captured_material_id": second_capture_id,
                        "artifact_ordinal": 0,
                        "exact_scope": "The complete second returned worker output.",
                        "coverage": "complete_artifact",
                    },
                ],
                "interpretation": (
                    "Two independent constructions report the same finite "
                    "obstruction at the tested scope."
                ),
                "scope": "Only the two preserved worker outputs.",
                "strength": "heuristic",
                "limitations": ["The agreement is not a general theorem."],
                "significance": (
                    "The shared obstruction becomes the next discriminator."
                ),
            },
            executive_epoch_id=epoch_id,
        )
        self.assertEqual(evidence["status"], "completed", evidence)
        evidence_id = evidence["result"]["records"][0]["record_id"]
        context_id = context["result"]["record_id"]

        strategy = self._execute(
            RECORD_STRATEGY,
            {
                "mission_continuation": "continue",
                "integrated_comparison": (
                    "The two selected outputs agree on one obstruction; continue "
                    "into the next discriminator."
                ),
                "causal_inputs": [
                    {
                        "source": {
                            "id": evidence_id,
                        },
                        "decision_consequence": (
                            "Promote the shared obstruction as the next discriminator."
                        ),
                    }
                ],
                "selected_bets": [
                    {
                        "bet": "Test whether the shared obstruction survives extension.",
                        "discriminator": (
                            "The two constructions either continue to agree or split."
                        ),
                        "owner_refs": [
                            {
                                "id": evidence_id,
                            }
                        ],
                    }
                ],
                "context_treatment": [
                    {
                        "context": {
                            "id": context_id,
                        },
                        "treatment": (
                            "Retain the shared question for the next discriminator."
                        ),
                    }
                ],
                "reconsideration_conditions": [
                    {
                        "condition": (
                            "Reconsider when the extension discriminator is resolved."
                        ),
                        "owner_refs": [
                            {
                                "id": evidence_id,
                            }
                        ],
                    }
                ],
                "owner_refs": [
                    {"id": context_id},
                    {"id": evidence_id},
                ],
            },
            executive_epoch_id=epoch_id,
        )
        self.assertEqual(strategy["status"], "completed", strategy)

        checkpointed = self._execute(
            CHECKPOINT,
            {},
            executive_epoch_id=epoch_id,
        )
        self.assertEqual(checkpointed["status"], "completed", checkpointed)
        checkpoint = self.store.read_continuation_checkpoint(
            executive_epoch_id=epoch_id
        )
        self.assertIsNotNone(checkpoint)
        assert checkpoint is not None
        unrelied_capture_id = str(unrelied["material_id"]).partition(":")[2]
        checkpoint_sections = self._checkpoint_sections(checkpoint)
        self.assertEqual(
            checkpoint_sections["pending_capture_locators"],
            [{"capture_id": unrelied_capture_id, "artifact_ordinal": 0}],
        )
        self.assertEqual(
            checkpoint_sections["causal_pointers"],
            [
                {
                    "owner_ref": checkpoint["document"]["strategy_root"],
                    "json_pointer": "/causal_inputs/0",
                }
            ],
        )
        transitive_kinds = {
            item["kind"]
            for item in checkpoint_sections["transitive_owner_refs"]
        }
        self.assertTrue({"context", "evidence"}.issubset(transitive_kinds))

    def test_exact_nine_reissue_authority_only_from_direct_store_owners(self) -> None:
        epoch_id = self._authorize()
        self._bind(epoch_id)
        capture_id = self._capture_pending_material(epoch_id)

        operations = (
            self._request(ORIENT, {}),
            self._request(
                RETRIEVE,
                {
                    "mode": "read",
                    "purpose": "Read one exact current direct owner revision.",
                    "ids": ["strategy:strategy.theta.1"],
                },
            ),
            self._request(
                RECORD_CONTEXT,
                {
                    "context_id": "context:context.direct-authority-route",
                    "subject": "Direct owner authority routing",
                    "question": "Which exact direct facts authorize this epoch?",
                    "material": [
                        {
                            "id": "mission:mission.1",
                            "why": "The current Mission head owns lifecycle meaning.",
                        }
                    ],
                },
            ),
            self._request(
                INTERPRET_MATERIAL,
                {
                    "evidence_id": "evidence:evidence.direct-authority-route",
                    "capture_scopes": [
                        {
                            "captured_material_id": f"capture:{capture_id}",
                            "artifact_ordinal": 0,
                            "exact_scope": "Every byte in the captured output artifact.",
                            "coverage": "complete_artifact",
                        }
                    ],
                    "interpretation": (
                        "The captured output states one pending factual result."
                    ),
                    "scope": "The exact captured artifact only.",
                    "strength": "heuristic",
                    "limitations": ["No mathematical generalization follows."],
                    "significance": "The result is available for exact comparison.",
                },
            ),
            self._request(
                RECORD_CANDIDATE,
                {
                    "candidate_id": "candidate:candidate.direct-authority-route",
                    "proposal_kind": "lemma",
                    "exact_statement": (
                        "The direct-authority fixture has one exact comparison target."
                    ),
                    "standing": {
                        "status": "open",
                        "basis": "The target remains to be tested.",
                    },
                },
            ),
            self._request(
                RECORD_BRANCH,
                {
                    "branch_id": "branch:branch.direct-authority-route",
                    "question": "Does the exact comparison target survive testing?",
                    "leverage_fingerprint": "direct-authority-route-comparison",
                    "target_hook": "Test the exact comparison target.",
                    "owner_refs": [
                        {
                            "id": "candidate:candidate.direct-authority-route",
                        }
                    ],
                },
            ),
            self._request(
                SYNTHESIZE,
                {
                    "relation_question": (
                        "How does the captured fact bear on the comparison target?"
                    ),
                    "inputs": [
                        {
                            "id": "evidence:evidence.direct-authority-route",
                            "role": "captured fact",
                        },
                        {
                            "id": "candidate:candidate.direct-authority-route",
                            "role": "comparison target",
                        },
                    ],
                    "compatibility_analysis": (
                        "Both meanings concern the same exact comparison fixture."
                    ),
                    "derivation_or_incompatibility": (
                        "No stronger consequence is justified by this fixture."
                    ),
                    "scope": "The two selected owner revisions only.",
                    "strength": "heuristic",
                    "edge_survival": "The exact comparison remains open.",
                    "consequences": [],
                },
            ),
            self._request(
                RECORD_STRATEGY,
                {
                    "mission_continuation": "continue",
                    "integrated_comparison": (
                        "The exact comparison remains the current useful discriminator."
                    ),
                    "reconsideration_conditions": [
                        {
                            "condition": "Reconsider when the comparison is resolved.",
                            "owner_refs": [],
                        }
                    ],
                },
            ),
            self._request(CHECKPOINT, {}),
        )
        mission_owner = TypedWorkspaceId(IdentityKind.MISSION, "mission.1")

        with (
            mock.patch.object(
                self.store, "get_head", wraps=self.store.get_head
            ) as mission_reads,
            mock.patch.object(
                self.store,
                "read_active_executive_epoch",
                wraps=self.store.read_active_executive_epoch,
            ) as epoch_reads,
            mock.patch.object(
                self.store, "read_metadata", wraps=self.store.read_metadata
            ) as metadata_reads,
            mock.patch.object(
                self.store,
                "read_root_retrieval_cut",
                wraps=self.store.read_root_retrieval_cut,
            ) as retrieval_cut_reads,
        ):
            for request in operations:
                with self.subTest(operation=request["operation"]):
                    before_mission = mission_reads.call_count
                    before_epoch = epoch_reads.call_count
                    before_metadata = metadata_reads.call_count
                    before_retrieval_cut = retrieval_cut_reads.call_count
                    response = self.interface.execute_semantic_operation(
                        request,
                        executive_epoch_id=epoch_id,
                    )
                    self.assertEqual(response["status"], "completed", response)
                    self.assertIn(
                        mock.call(mission_owner),
                        mission_reads.call_args_list[before_mission:],
                    )
                    self.assertIn(
                        mock.call("mission.1"),
                        epoch_reads.call_args_list[before_epoch:],
                    )
                    if request["operation"] == RETRIEVE:
                        self.assertIn(
                            mock.call(),
                            retrieval_cut_reads.call_args_list[
                                before_retrieval_cut:
                            ],
                        )
                        self.assertEqual(
                            metadata_reads.call_args_list[before_metadata:],
                            [],
                        )
                    else:
                        self.assertIn(
                            mock.call(),
                            metadata_reads.call_args_list[before_metadata:],
                        )
                        self.assertEqual(
                            retrieval_cut_reads.call_args_list[
                                before_retrieval_cut:
                            ],
                            [],
                        )

    @staticmethod
    def _argument_authority_candidate(candidate_id, authority_class, refs=None):
        authority = {"class": authority_class}
        if refs is not None:
            authority["refs"] = refs
        return {
            "candidate_id": f"candidate:{candidate_id}",
            "proposal_kind": "lemma",
            "exact_statement": "A fixture-only bounded implication remains unproved.",
            "standing": {"status": "open", "basis": "Requires independent checking."},
            "argument_edges": [{
                "edge_id": "edge.fixture",
                "edge_kind": "argument",
                "premises": ["The exact fixture hypothesis holds."],
                "conclusion": "The bounded fixture implication follows.",
                "authority": authority,
            }],
        }

    def test_argument_authority_classes_bind_dependencies_and_read_losslessly(self):
        from research_core import mission_interface as owner

        epoch = self._authorize()
        self._bind(epoch)
        capture_id = self._capture_pending_material(epoch)
        evidence_id = "evidence:evidence.argument-authority"
        evidence = self._execute(INTERPRET_MATERIAL, {
            "evidence_id": evidence_id,
            "capture_scopes": [{"captured_material_id": f"capture:{capture_id}",
                "artifact_ordinal": 0, "exact_scope": "The complete fixture artifact.",
                "coverage": "complete_artifact"}],
            "interpretation": "One fixture-only premise is available.",
            "scope": "The fixture only.", "strength": "heuristic",
            "limitations": ["No mathematical generalization."],
            "significance": "An exact interpreted reference for this regression.",
        }, executive_epoch_id=epoch)
        self.assertEqual(evidence["status"], "completed", evidence)
        exact_evidence, dependency = self.interface._resolve_owner_selector(
            {"id": evidence_id, "revision": 1}
        )
        candidate_inputs = []
        first_read = None
        for authority_class in (
            "candidate_only", "evidence_interpretation",
            "canonical_mathematics", "formal_verification",
        ):
            with self.subTest(authority_class=authority_class):
                refs = None if authority_class == "candidate_only" else [
                    {"id": evidence_id, "revision": 1}
                ]
                candidate = self._argument_authority_candidate(
                    f"candidate.argument-{authority_class}", authority_class, refs
                )
                second_edge = deep_thaw(candidate["argument_edges"][0])
                second_edge["edge_id"] = "edge.fixture.second"
                candidate["argument_edges"].append(second_edge)
                with mock.patch.object(owner, "commit_candidate_revision",
                        wraps=owner.commit_candidate_revision) as committed:
                    result = self._execute(RECORD_CANDIDATE, candidate,
                        executive_epoch_id=epoch)
                self.assertEqual(result["status"], "completed", result)
                self.assertIsNone(result["result"]["open_candidate_a1"])
                self.assertEqual(committed.call_count, 1)
                self.assertEqual(committed.call_args.kwargs["expected_dependency_heads"],
                    {} if refs is None else {dependency[0]: dependency[1]})
                stored = self.store.read_candidate_revision(mission_id="mission.1",
                    candidate_id=candidate["candidate_id"].split(":", 1)[1])
                edge = deep_thaw(stored["payload"]["argument_edges"])[0]
                if refs is not None:
                    self.assertEqual(edge["authority"]["refs"], [{
                        "kind": "evidence", "id": exact_evidence["identity"],
                        "revision": 1, "digest_sha256": exact_evidence["payload_sha256"],
                    }])
                else:
                    self.assertEqual(edge, candidate["argument_edges"][0])
                read = self._execute(RETRIEVE, {"mode": "read",
                    "purpose": "Read the exact retained argument edge.",
                    "ids": [candidate["candidate_id"] + "@1"]}, executive_epoch_id=epoch)
                self.assertEqual(read["status"], "completed", read)
                readable = read["result"]["items"][0]["readable_content"]
                self.assertEqual(json.loads(readable)["argument_edges"], candidate["argument_edges"])
                candidate_inputs.append(candidate)
                if first_read is None:
                    first_read = readable

        self.assertEqual(len(candidate_inputs), 4)
        revised = {**candidate_inputs[0], "gaps": ["A later precise gap."]}
        revision = self._execute(RECORD_CANDIDATE, revised, executive_epoch_id=epoch)
        self.assertEqual(revision["result"]["revision"], 2)
        retained = self._execute(RETRIEVE, {"mode": "read",
            "purpose": "Confirm historical argument meaning is immutable.",
            "ids": [revised["candidate_id"] + "@1"]}, executive_epoch_id=epoch)
        self.assertEqual(retained["result"]["items"][0]["readable_content"], first_read)
        strategy = self._execute(RECORD_STRATEGY, {
            "mission_continuation": "continue", "integrated_comparison": "Compare exact fixture arguments.",
            "reconsideration_conditions": [{"condition": "When a fixture implication is checked.", "owner_refs": []}],
            "owner_refs": [{"id": item["candidate_id"]} for item in candidate_inputs],
        }, executive_epoch_id=epoch)
        self.assertEqual(strategy["status"], "completed", strategy)
        orientation = self.interface.executive_orientation()
        self.assertNotIn("strategy_ground", orientation)
        self.assertEqual(
            {reference["id"] for reference in orientation["current_strategy"]["owner_refs"]},
            {item["candidate_id"] for item in candidate_inputs},
        )

    def test_argument_authority_exception_rejects_nested_owner_facts_and_extra_fields(self):
        epoch = self._authorize()
        self._bind(epoch)
        base = self._argument_authority_candidate("candidate.argument-rejected", "candidate_only")
        cases = []
        for key in ("authority", "_authority", "Authority"):
            cases.append(({**base, key: {}}, "mission_operation_owner_fact_forbidden"))
            bad = deep_thaw(base)
            bad["standing"][key] = {}
            cases.append((bad, "mission_operation_owner_fact_forbidden"))
            bad = deep_thaw(base)
            bad["argument_edges"][0]["authority"][key] = {}
            cases.append((bad, "mission_operation_owner_fact_forbidden"))
        for key in ("actor", "principal", "command_id", "lease", "writer_lease",
                    "project_commit", "provider", "canonical_authority_digest", "payload_sha256"):
            bad = deep_thaw(base)
            bad["argument_edges"][0]["authority"][key] = "forged"
            cases.append((bad, "mission_operation_owner_fact_forbidden"))
        for key in ("source_commit", "payload_digest", "canonical_authority", "extra"):
            bad = deep_thaw(base)
            bad["argument_edges"][0]["authority"][key] = {}
            cases.append((bad, "mission_operation_request_invalid"))
        for key in ("authority", "canonical_authority", "actor", "payload_sha256"):
            bad = deep_thaw(base)
            bad["argument_edges"][0]["authority"]["refs"] = [
                {"id": "branch:branch.theta", key: {}}
            ]
            cases.append((bad, "mission_operation_request_invalid" if key == "canonical_authority"
                          else "mission_operation_owner_fact_forbidden"))
        for key in ("_authority", "Authority"):
            bad = deep_thaw(base)
            bad["argument_edges"][0][key] = bad["argument_edges"][0].pop("authority")
            cases.append((bad, "mission_operation_owner_fact_forbidden"))
        duplicate = deep_thaw(base)
        duplicate["argument_edges"].append(deep_thaw(duplicate["argument_edges"][0]))
        cases.append((duplicate, "mission_operation_request_invalid"))
        before = (deep_thaw(self.store.read_metadata()), self.store.paths.database.read_bytes())
        for semantic_input, code in cases:
            synthesis_input = {
                "relation_question": "Reject malformed consequences before any owner effect.",
                "inputs": [{"id": "candidate:unused-one", "role": "first"},
                           {"id": "candidate:unused-two", "role": "second"}],
                "compatibility_analysis": "Fixture-only validation.",
                "derivation_or_incompatibility": "No valid consequence is requested.",
                "scope": "The fixture only.", "strength": "heuristic",
                "edge_survival": "Not evaluated.",
                "consequences": [{"kind": "candidate", "candidate": semantic_input}],
            }
            for operation, payload in ((RECORD_CANDIDATE, semantic_input), (SYNTHESIZE, synthesis_input)):
                with self.subTest(operation=operation, semantic_input=semantic_input):
                    result = self._execute(operation, payload, executive_epoch_id=epoch)
                    self.assertEqual(result["status"], "rejected", result)
                    self.assertEqual(result["error"]["code"], code, result)
        unrelated = self._request(ORIENT, {"argument_edges": base["argument_edges"]})
        top = {**self._request(RECORD_CANDIDATE, base), "authority": {}}
        for request in (unrelated, top):
            result = self.interface.execute_semantic_operation(request, executive_epoch_id=epoch)
            self.assertEqual(result["error"]["code"], "mission_operation_owner_fact_forbidden", result)
        self.assertEqual((deep_thaw(self.store.read_metadata()), self.store.paths.database.read_bytes()), before)

    def test_argument_authority_complete_claim_a1_is_symmetric_and_revision_bound(self):
        epoch = self._authorize()
        self._bind(epoch)
        canonical_before = self.canonical.authorized_path.read_bytes()
        for disposition in ("proof", "disproof"):
            with self.subTest(disposition=disposition):
                candidate_id = f"candidate.argument-complete-{disposition}"
                candidate = self._argument_authority_candidate(candidate_id, "candidate_only")
                candidate["complete_target_claim"] = {"target": "riemann_hypothesis", "disposition": disposition}
                result = self._execute(RECORD_CANDIDATE, candidate, executive_epoch_id=epoch)
                self.assertEqual(result["status"], "completed", result)
                stored = self.store.read_candidate_revision(mission_id="mission.1", candidate_id=candidate_id)
                a1 = result["result"]["open_candidate_a1"]
                self.assertEqual(a1["disposition"], disposition)
                self.assertEqual(a1["hold_lifecycle"], "open")
                self.assertEqual(a1["candidate_ref"]["revision"], stored["revision"])
                self.assertEqual(a1["candidate_ref"]["payload_sha256"], stored["payload_digest"])
                binding = self.store.read_candidate_a1_binding(candidate_id=candidate_id,
                    candidate_revision=stored["revision"], candidate_digest=stored["payload_digest"])
                binding.verify_issued()
                self.assertEqual(binding.hold_lifecycle, "open")
                self.assertEqual(binding.canonical_effect, "none")
                self.assertEqual(binding.mathematical_effect, "none")
                self.assertEqual(self._execute(RECORD_CANDIDATE, candidate, executive_epoch_id=epoch), result)
                self.assertEqual(deep_thaw(stored["payload"]["argument_edges"]), candidate["argument_edges"])
        self.assertEqual(len(self.interface.reconstruct()["recovery_opening"]["open_candidate_a1"]), 2)
        self.assertEqual(self.canonical.authorized_path.read_bytes(), canonical_before)

    def test_historical_revision_reads_and_synthesis_rejections_are_local(self) -> None:
        epoch_id = self._authorize()
        self._bind(epoch_id)
        argument_edges = self._argument_authority_candidate(
            "candidate.synthesis-fixture", "candidate_only"
        )["argument_edges"]
        candidate_a_input = {
            "candidate_id": "candidate:candidate.direct-history.a",
            "proposal_kind": "lemma",
            "exact_statement": "The direct historical fixture has exact revision one.",
            "standing": {
                "status": "open",
                "basis": "The fixture is intentionally unresolved.",
            },
            "non_inferences": ["This does not establish RH."],
        }
        candidate_b_input = {
            "candidate_id": "candidate:candidate.direct-history.b",
            "proposal_kind": "mechanism",
            "exact_statement": "A distinct direct mechanism remains available.",
            "mechanism": "Test the distinct mechanism against the exact observation.",
            "standing": {
                "status": "open",
                "basis": "The mechanism remains to be tested.",
            },
            "non_inferences": ["This does not establish the mechanism or RH."],
        }
        candidate_a_response = self._execute(
            RECORD_CANDIDATE,
            candidate_a_input,
            executive_epoch_id=epoch_id,
        )
        candidate_b_response = self._execute(
            RECORD_CANDIDATE,
            candidate_b_input,
            executive_epoch_id=epoch_id,
        )
        self.assertEqual(
            candidate_a_response["status"], "completed", candidate_a_response
        )
        self.assertEqual(
            candidate_b_response["status"], "completed", candidate_b_response
        )
        candidate_a = candidate_a_response["result"]
        candidate_b = candidate_b_response["result"]
        self.assertEqual(candidate_a["revision"], 1)
        self.assertEqual(candidate_b["revision"], 1)

        synthesis = self._execute(
            SYNTHESIZE,
            {
                "relation_question": (
                    "Does the distinct mechanism survive the revision-one observation?"
                ),
                "inputs": [
                    {
                        "id": candidate_a["record_id"],
                        "revision": candidate_a["revision"],
                        "role": "observation",
                    },
                    {
                        "id": candidate_b["record_id"],
                        "revision": candidate_b["revision"],
                        "role": "mechanism",
                    },
                ],
                "compatibility_analysis": (
                    "The two exact Candidate meanings are compatible but inconclusive."
                ),
                "derivation_or_incompatibility": (
                    "The observation does not exclude the distinct mechanism."
                ),
                "scope": "The two selected Candidate revisions only.",
                "strength": "heuristic",
                "dependencies": [{"id": "branch:branch.theta"}],
                "edge_survival": "The distinct mechanism remains open.",
                "consequences": [
                    {
                        "kind": "evidence",
                        "evidence_id": "evidence:evidence.direct-local-consequence",
                        "statement": (
                            "The revision-one observation does not exclude the mechanism."
                        ),
                        "scope": "The two selected Candidate revisions.",
                        "strength": "heuristic",
                        "semantic_role": "non_exclusion",
                        "limitations": ["No mechanism is proved."],
                        "non_inferences": ["Do not infer success."],
                        "decision_consequence": "The mechanism remains available.",
                    },
                    {
                        "kind": "candidate",
                        "candidate": {
                            "candidate_id": "branch:not-a-candidate",
                            "proposal_kind": "lemma",
                            "exact_statement": (
                                "This deliberately targets the wrong owner kind."
                            ),
                            "standing": {
                                "status": "open",
                                "basis": "It proves consequence-local rejection.",
                            },
                            "argument_edges": argument_edges,
                        },
                    },
                    {
                        "kind": "candidate",
                        "candidate": {
                            "candidate_id": "candidate:derived-local-consequence",
                            "proposal_kind": "lemma",
                            "exact_statement": (
                                "The exact fixture inputs retain one bounded "
                                "derived comparison target."
                            ),
                            "standing": {
                                "status": "open",
                                "basis": "The bounded target remains to be tested.",
                            },
                            "non_inferences": ["Do not infer RH."],
                            "argument_edges": argument_edges,
                        },
                    },
                    {
                        "kind": "candidate",
                        "candidate": self._argument_authority_candidate(
                            "candidate.synthesis-bad-edge-selector",
                            "evidence_interpretation",
                            [{"id": "not-a-typed-owner"}],
                        ),
                    },
                ],
            },
            executive_epoch_id=epoch_id,
        )
        self.assertEqual(synthesis["status"], "completed", synthesis)
        self.assertEqual(len(synthesis["result"]["records"]), 2)
        self.assertEqual(len(synthesis["result"]["rejections"]), 2)
        self.assertEqual(
            synthesis["result"]["rejections"][0]["consequence_index"],
            1,
        )
        self.assertEqual(synthesis["result"]["rejections"][1]["consequence_index"], 3)
        self.assertEqual(
            synthesis["result"]["records"][0]["record_id"],
            "evidence:evidence.direct-local-consequence",
        )
        derived = read_evidence_meaning(
            self.store,
            evidence_id="evidence.direct-local-consequence",
        )
        relation = derived.evidence.subject["dependencies"][0]
        self.assertEqual(
            relation["dependencies"][0]["kind"],
            "branch",
        )
        self.assertEqual(
            relation["dependencies"][0]["identity"],
            "branch.theta",
        )
        derived_candidate = self.store.read_candidate_revision(
            mission_id="mission.1",
            candidate_id="derived-local-consequence",
        )
        supporting_refs = derived_candidate["payload"]["supporting_refs"]
        self.assertEqual(deep_thaw(derived_candidate["payload"]["argument_edges"]), argument_edges)
        consequence_read = self._execute(RETRIEVE, {
            "mode": "read", "purpose": "Read the exact successful synthesis argument.",
            "ids": ["candidate:derived-local-consequence@1"],
        }, executive_epoch_id=epoch_id)
        self.assertEqual(consequence_read["status"], "completed", consequence_read)
        self.assertEqual(json.loads(consequence_read["result"]["items"][0]["readable_content"])["argument_edges"], argument_edges)
        self.assertTrue(
            any(
                item["kind"] == "branch"
                and item["id"] == "branch.theta"
                and item["revision"] == 1
                for item in supporting_refs
            )
        )

        revised_a_input = dict(candidate_a_input)
        revised_a_input["gaps"] = [
            "This sentence belongs only to Candidate revision two."
        ]
        revised_a = self._execute(
            RECORD_CANDIDATE,
            revised_a_input,
            executive_epoch_id=epoch_id,
        )
        self.assertEqual(revised_a["result"]["revision"], 2)
        historical = self._execute(
            RETRIEVE,
            {
                "mode": "read",
                "purpose": "Read the exact first Candidate revision.",
                "ids": [candidate_a["record_id"] + "@1"],
            },
            executive_epoch_id=epoch_id,
        )
        self.assertEqual(historical["status"], "completed", historical)
        self.assertEqual(
            historical["result"]["items"][0]["id"],
            candidate_a["record_id"] + "@1",
        )
        for noncanonical in (
            candidate_a["record_id"] + "@01",
            candidate_a["record_id"] + "@\u0661",
        ):
            with self.subTest(noncanonical=noncanonical):
                with self.assertRaisesRegex(ValueError, "historical record ids"):
                    self.interface._parse_retrieval_id(noncanonical)
        self.assertNotIn(
            "This sentence belongs only to Candidate revision two.",
            historical["result"]["items"][0]["readable_content"],
        )

        mixed = self._execute(
            SYNTHESIZE,
            {
                "relation_question": (
                    "How does the revised observation differ from its exact first revision?"
                ),
                "inputs": [
                    {
                        "id": candidate_a["record_id"],
                        "role": "current observation",
                    },
                    {
                        "id": candidate_a["record_id"],
                        "revision": candidate_a["revision"],
                        "role": "historical observation",
                    },
                ],
                "compatibility_analysis": (
                    "The two exact revisions are comparable and remain distinct."
                ),
                "derivation_or_incompatibility": (
                    "Revision two adds one gap while revision one remains unchanged."
                ),
                "scope": "Candidate A revisions one and two only.",
                "strength": "exact_finite_identity",
                "edge_survival": "The exact revision distinction survives.",
                "consequences": [
                    {
                        "kind": "evidence",
                        "evidence_id": "evidence:evidence.direct-mixed-revisions",
                        "statement": (
                            "Candidate A revision two differs from exact revision one."
                        ),
                        "scope": "Candidate A revisions one and two only.",
                        "strength": "exact_finite_identity",
                        "semantic_role": "revision_comparison",
                        "non_inferences": ["Do not infer any mathematical claim."],
                    }
                ],
            },
            executive_epoch_id=epoch_id,
        )
        self.assertEqual(mixed["status"], "completed", mixed)
        self.assertEqual(len(mixed["result"]["records"]), 1)
        self.assertEqual(mixed["result"]["rejections"], [])
        mixed_evidence = read_evidence_meaning(
            self.store,
            evidence_id="evidence.direct-mixed-revisions",
        )
        self.assertEqual(
            {item.revision for item in mixed_evidence.interpreted_inputs},
            {1, 2},
        )

    def test_record_candidate_result_surfaces_exact_open_a1_state(self) -> None:
        epoch_id = self._authorize()
        self._bind(epoch_id)

        ordinary = self._execute(
            RECORD_CANDIDATE,
            {
                "candidate_id": "candidate:candidate.direct-result-ordinary",
                "proposal_kind": "lemma",
                "exact_statement": (
                    "The direct result fixture has one bounded local lemma."
                ),
                "scope_and_reach": "one bounded local lemma",
                "standing": {
                    "status": "open",
                    "basis": "The local lemma remains to be tested.",
                },
                "non_inferences": ["Do not infer a complete RH result."],
            },
            executive_epoch_id=epoch_id,
        )
        self.assertEqual(ordinary["status"], "completed", ordinary)
        self.assertIsNone(ordinary["result"]["open_candidate_a1"])

        purported_complete_input = {
            "candidate_id": "candidate:candidate.direct-result-complete-rh",
            "proposal_kind": "proof_architecture",
            "exact_statement": (
                "Every nontrivial zero of the Riemann zeta function lies on "
                "the critical line."
            ),
            "mechanism": "A purported unconditional argument proves RH.",
            "scope_and_reach": "complete proof of the Riemann Hypothesis",
            "complete_target_claim": {
                "target": "riemann_hypothesis",
                "disposition": "proof",
            },
            "standing": {
                "status": "open",
                "basis": "The purported proof requires adversarial review.",
            },
            "non_inferences": [
                "Do not infer that the purported proof has survived review."
            ],
        }
        purported_complete = self._execute(
            RECORD_CANDIDATE,
            purported_complete_input,
            executive_epoch_id=epoch_id,
        )
        self.assertEqual(
            purported_complete["status"], "completed", purported_complete
        )
        complete_stored = self.store.read_candidate_revision(
            mission_id="mission.1",
            candidate_id="candidate.direct-result-complete-rh",
        )
        self.assertEqual(
            purported_complete["result"]["open_candidate_a1"],
            {
                "candidate_ref": {
                    "kind": "candidate",
                    "identity": "candidate.direct-result-complete-rh",
                    "revision": 1,
                    "payload_sha256": complete_stored["payload_digest"],
                },
                "retrieval_handle": (
                    "candidate:candidate.direct-result-complete-rh@1"
                ),
                "classification": "purported_complete_rh_proof_or_disproof",
                "disposition": "proof",
                "hold_lifecycle": "open",
            },
        )

        replay = self._execute(
            RECORD_CANDIDATE,
            purported_complete_input,
            executive_epoch_id=epoch_id,
        )
        self.assertEqual(replay, purported_complete)

    def test_record_candidate_v3_uses_only_structured_complete_target_claim(self) -> None:
        epoch_id = self._authorize()
        self._bind(epoch_id)

        text_only = self._execute(
            RECORD_CANDIDATE,
            {
                "candidate_id": "candidate:candidate.direct-v3-text-only",
                "proposal_kind": "proof_architecture",
                "exact_statement": "This purported argument proves RH.",
                "mechanism": "A text-only purported proof architecture.",
                "scope_and_reach": "complete proof of the Riemann Hypothesis",
                "standing": {
                    "status": "open",
                    "basis": "The text-only scope remains an ordinary v3 Candidate.",
                },
            },
            executive_epoch_id=epoch_id,
        )
        self.assertEqual(text_only["status"], "completed", text_only)
        self.assertIsNone(text_only["result"]["open_candidate_a1"])
        text_only_stored = self.store.read_candidate_revision(
            mission_id="mission.1",
            candidate_id="candidate.direct-v3-text-only",
        )
        self.assertEqual(text_only_stored["payload"]["schema_version"], 3)
        self.assertNotIn("complete_target_claim", text_only_stored["payload"])

        disproof = self._execute(
            RECORD_CANDIDATE,
            {
                "candidate_id": "candidate:candidate.direct-v3-disproof",
                "proposal_kind": "mathematical_statement",
                "exact_statement": "One exact target-facing result requires review.",
                "standing": {
                    "status": "open",
                    "basis": "No proof material is required in this semantic request.",
                },
                "complete_target_claim": {
                    "target": "riemann_hypothesis",
                    "disposition": "disproof",
                },
            },
            executive_epoch_id=epoch_id,
        )
        self.assertEqual(disproof["status"], "completed", disproof)
        disproof_a1 = disproof["result"]["open_candidate_a1"]
        self.assertEqual(disproof_a1["disposition"], "disproof")
        self.assertEqual(
            disproof_a1["retrieval_handle"],
            "candidate:candidate.direct-v3-disproof@1",
        )
        disproof_stored = self.store.read_candidate_revision(
            mission_id="mission.1",
            candidate_id="candidate.direct-v3-disproof",
        )
        self.assertEqual(
            disproof_a1["candidate_ref"]["payload_sha256"],
            disproof_stored["payload_digest"],
        )
        self.assertEqual(
            set(disproof["result"]),
            {"record_id", "revision", "semantic_summary", "open_candidate_a1"},
        )

    def test_record_candidate_advances_legacy_v2_to_v3_without_regex_a1(self) -> None:
        epoch_id = self._authorize()
        self._bind(epoch_id)
        authority = self.interface._direct_epoch_authority(epoch_id)
        legacy_v2 = _prepare_candidate_revision_v2_legacy_document(
            authority=authority,
            candidate_id="candidate.direct-v2-upgrade",
            proposal_kind="proof_architecture",
            exact_statement="This purported argument proves RH.",
            mechanism="A legacy purported proof architecture.",
            standing={
                "status": "open",
                "basis": "Legacy v2 uses its historical conservative detector.",
            },
            scope_and_reach="complete proof of the Riemann Hypothesis",
            repo_root=REPO_ROOT,
        )
        metadata = self.store.read_metadata()
        legacy_payload = deep_thaw(legacy_v2.document)
        legacy_digest = canonical_payload(legacy_payload).sha256
        auxiliary_writes, evidence_advances = (
            self.store._prepare_successor_candidate_a1_projection(
                candidate_id="candidate.direct-v2-upgrade",
                candidate_payload=legacy_payload,
                candidate_digest=legacy_digest,
                actor="test.direct-v2-upgrade",
                expected_canonical_authority_digest=str(
                    metadata["canonical_authority_digest"]
                ),
            )
        )
        legacy_write = RevisionWrite(
            TypedWorkspaceId(
                IdentityKind.CANDIDATE, "candidate.direct-v2-upgrade"
            ),
            legacy_payload,
            expected_revision=None,
        )
        # Materialize retained v2 history through the explicit fixture-only
        # storage primitive, never through the current Candidate write API.
        with mock.patch.object(
            self.store,
            "_validate_current_successor_candidate_a1",
            return_value=None,
        ):
            self.store._apply_successor_storage_command(
                family=RevisionCommandFamily.CANDIDATE,
                lease=self.interface._writer_lease(),
                command_id="test.direct-v2-upgrade.historical-fixture",
                actor="test.direct-v2-upgrade",
                command_kind="commit_candidate_revision",
                executive_epoch_id=epoch_id,
                request={
                    "mission_id": "mission.1",
                    "candidate_id": "candidate.direct-v2-upgrade",
                    "historical_fixture": True,
                },
                semantic_payload=legacy_payload,
                writes=(legacy_write,),
                auxiliary_writes=auxiliary_writes,
                evidence_head_advances=evidence_advances,
                target_head_key=legacy_write.object_id.key,
                expected_target_revision=None,
                expected_target_payload_digest=None,
                dependency_heads=None,
                expected_canonical_authority_digest=str(
                    metadata["canonical_authority_digest"]
                ),
            )
        stored_v2 = self.store.read_candidate_revision(
            mission_id="mission.1",
            candidate_id="candidate.direct-v2-upgrade",
        )
        self.assertEqual(stored_v2["payload"]["schema_version"], 2)
        legacy_projection = self.interface.reconstruct()["recovery_opening"][
            "open_candidate_a1"
        ]
        self.assertEqual(len(legacy_projection), 1)
        self.assertEqual(legacy_projection[0]["disposition"], "legacy_unspecified")

        upgraded = self._execute(
            RECORD_CANDIDATE,
            {
                "candidate_id": "candidate:candidate.direct-v2-upgrade",
                "proposal_kind": "proof_architecture",
                "exact_statement": "This purported argument proves RH.",
                "mechanism": "A legacy purported proof architecture.",
                "standing": {
                    "status": "open",
                    "basis": "Legacy v2 uses its historical conservative detector.",
                },
                "scope_and_reach": "complete proof of the Riemann Hypothesis",
            },
            executive_epoch_id=epoch_id,
        )
        self.assertEqual(upgraded["status"], "completed", upgraded)
        self.assertEqual(upgraded["result"]["revision"], 2)
        self.assertIsNone(upgraded["result"]["open_candidate_a1"])
        current = self.store.read_candidate_revision(
            mission_id="mission.1",
            candidate_id="candidate.direct-v2-upgrade",
        )
        self.assertEqual(current["payload"]["schema_version"], 3)
        self.assertNotIn("complete_target_claim", current["payload"])
        retained = self.interface.reconstruct()["recovery_opening"][
            "open_candidate_a1"
        ]
        self.assertEqual(len(retained), 1)
        self.assertEqual(retained[0]["candidate_ref"]["revision"], 1)
        self.assertEqual(retained[0]["disposition"], "legacy_unspecified")

    def test_purported_complete_rh_candidate_atomically_opens_a1(self) -> None:
        epoch_id = self._authorize()
        self._bind(epoch_id)
        candidate_input = {
            "candidate_id": "candidate:candidate.direct-complete-rh",
            "proposal_kind": "proof_architecture",
            "exact_statement": (
                "Every nontrivial zero of the Riemann zeta function lies on "
                "the critical line."
            ),
            "mechanism": (
                "A purported unconditional argument proves the exact statement."
            ),
            "scope_and_reach": "complete proof of the Riemann Hypothesis",
            "complete_target_claim": {
                "target": "riemann_hypothesis",
                "disposition": "proof",
            },
            "standing": {
                "status": "open",
                "basis": "The purported complete argument requires adversarial review.",
            },
            "non_inferences": [
                "Do not infer that the purported proof has survived review."
            ],
        }
        committed = self._execute(
            RECORD_CANDIDATE,
            candidate_input,
            executive_epoch_id=epoch_id,
        )
        self.assertEqual(committed["status"], "completed", committed)
        self.assertEqual(committed["result"]["revision"], 1)
        stored = self.store.read_candidate_revision(
            mission_id="mission.1",
            candidate_id="candidate.direct-complete-rh",
        )
        binding = self.store.read_candidate_a1_binding(
            candidate_id="candidate.direct-complete-rh",
            candidate_revision=int(stored["revision"]),
            candidate_digest=str(stored["payload_digest"]),
        )
        binding.verify_issued()
        self.assertEqual(binding.artifact_digest, stored["payload_digest"])
        self.assertEqual(binding.hold_lifecycle, "open")
        self.assertEqual(binding.canonical_effect, "none")
        self.assertEqual(binding.mathematical_effect, "none")

        replay = self._execute(
            RECORD_CANDIDATE,
            candidate_input,
            executive_epoch_id=epoch_id,
        )
        self.assertEqual(replay["result"]["revision"], 1)
        replayed_binding = self.store.read_candidate_a1_binding(
            candidate_id="candidate.direct-complete-rh",
            candidate_revision=int(stored["revision"]),
            candidate_digest=str(stored["payload_digest"]),
        )
        self.assertEqual(replayed_binding.digest_sha256, binding.digest_sha256)

        reconstruction = self.interface.reconstruct()
        candidate_delta = next(
            item
            for item in reconstruction["recovery_opening"]["owner_deltas"]
            if item["current_reference"]["kind"] == "candidate"
            and item["current_reference"]["identity"]
            == "candidate.direct-complete-rh"
        )
        self.assertEqual(
            candidate_delta["summary"]["candidate_a1"],
            {
                "candidate_ref": candidate_delta["current_reference"],
                "classification": "purported_complete_rh_proof_or_disproof",
                "disposition": "proof",
                "hold_lifecycle": "open",
                "canonical_effect": "none",
                "mathematical_effect": "none",
            },
        )
        self.assertEqual(
            reconstruction["recovery_opening"]["open_candidate_a1"],
            [
                {
                    "candidate_ref": candidate_delta["current_reference"],
                    "retrieval_handle": (
                        "candidate:candidate.direct-complete-rh@1"
                    ),
                    "classification": "purported_complete_rh_proof_or_disproof",
                    "disposition": "proof",
                    "hold_lifecycle": "open",
                    "canonical_effect": "none",
                    "mathematical_effect": "none",
                }
            ],
        )

        rejected_without_strategy_causality = self._execute(
            CHECKPOINT,
            {},
            executive_epoch_id=epoch_id,
        )
        self.assertEqual(rejected_without_strategy_causality["status"], "rejected")
        self.assertIn(
            "Strategy causal_inputs",
            rejected_without_strategy_causality["error"]["message"],
        )

        continued_without_candidate = self._execute(
            RECORD_STRATEGY,
            {
                "mission_continuation": "continue",
                "integrated_comparison": (
                    "A purported complete RH proof requires directed independent "
                    "reconstruction rather than broad exploration."
                ),
                "reconsideration_conditions": [
                    {
                        "condition": (
                            "Reconsider broad exploration only after the exact Candidate "
                            "has been independently reconstructed or falsified."
                        ),
                        "owner_refs": [],
                    }
                ],
            },
            executive_epoch_id=epoch_id,
        )
        self.assertEqual(continued_without_candidate["status"], "completed")
        rejected_without_causal_input = self._execute(
            CHECKPOINT,
            {},
            executive_epoch_id=epoch_id,
        )
        self.assertEqual(rejected_without_causal_input["status"], "rejected")
        self.assertIn(
            "Strategy causal_inputs",
            rejected_without_causal_input["error"]["message"],
        )

        continued_with_candidate = self._execute(
            RECORD_STRATEGY,
            {
                "mission_continuation": "continue",
                "integrated_comparison": (
                    "The exact purported complete RH Candidate is preserved and "
                    "work continues as directed verification."
                ),
                "causal_inputs": [
                    {
                        "source": {
                            "id": committed["result"]["record_id"],
                        },
                        "decision_consequence": (
                            "Pivot broad exploration and independently reconstruct "
                            "or falsify this exact Candidate."
                        ),
                    }
                ],
                "reconsideration_conditions": [
                    {
                        "condition": (
                            "Reconsider only after the exact Candidate has been "
                            "independently reconstructed or falsified."
                        ),
                        "owner_refs": [
                            {
                                "id": committed["result"]["record_id"],
                            }
                        ],
                    }
                ],
            },
            executive_epoch_id=epoch_id,
        )
        self.assertEqual(continued_with_candidate["status"], "completed")
        checkpointed = self._execute(
            CHECKPOINT,
            {},
            executive_epoch_id=epoch_id,
        )
        self.assertEqual(checkpointed["status"], "completed", checkpointed)

        self.store.verify_integrity()

    def test_candidate_a1_recovery_and_checkpoint_are_revision_aware(self) -> None:
        epoch_id = self._authorize()
        self._bind(epoch_id)
        candidate_input = {
            "candidate_id": "candidate:candidate.direct-complete-rh-history",
            "proposal_kind": "proof_architecture",
            "exact_statement": (
                "Every nontrivial zero of the Riemann zeta function lies on "
                "the critical line."
            ),
            "mechanism": "Purported complete argument revision one.",
            "scope_and_reach": "complete proof of the Riemann Hypothesis",
            "complete_target_claim": {
                "target": "riemann_hypothesis",
                "disposition": "proof",
            },
            "standing": {
                "status": "open",
                "basis": "Independent reconstruction is still required.",
            },
            "non_inferences": ["Do not infer that RH is proved."],
        }
        revision_one = self._execute(
            RECORD_CANDIDATE,
            candidate_input,
            executive_epoch_id=epoch_id,
        )
        revision_two_input = dict(candidate_input)
        revision_two_input["gaps"] = [
            "Revision two adds an explicit reconstruction obligation."
        ]
        revision_two = self._execute(
            RECORD_CANDIDATE,
            revision_two_input,
            executive_epoch_id=epoch_id,
        )
        self.assertEqual(revision_one["result"]["revision"], 1)
        self.assertEqual(revision_two["result"]["revision"], 2)

        for ordinal in range(12):
            ordinary = self._execute(
                RECORD_CANDIDATE,
                {
                    "candidate_id": (
                        f"candidate:candidate.a1-unrelated-history.{ordinal:03d}"
                    ),
                    "proposal_kind": "lemma",
                    "exact_statement": (
                        f"Synthetic unrelated retained lemma {ordinal}."
                    ),
                    "standing": {
                        "status": "open",
                        "basis": "Candidate-heavy retained-history scaling fixture.",
                    },
                },
                executive_epoch_id=epoch_id,
            )
            self.assertEqual(ordinary["status"], "completed", ordinary)
            self.assertIsNone(ordinary["result"]["open_candidate_a1"])

        bindings = self.store.list_mission_candidate_a1_bindings("mission.1")
        self.assertEqual(
            [(item.candidate_revision, item.hold_lifecycle) for item in bindings],
            [(1, "open"), (2, "open")],
        )
        review_context = self._execute(
            RECORD_CONTEXT,
            {
                "context_id": "context:context.a1-historical-open-review",
                "subject": "The earlier exact Candidate A1",
                "question": "Does revision one survive independent review?",
                "material": [{
                    "id": "candidate:candidate.direct-complete-rh-history",
                    "why": "Revision one remains an OPEN current restriction.",
                }],
            },
            executive_epoch_id=epoch_id,
        )
        self.assertEqual(review_context["status"], "completed", review_context)
        first_ref = revision_one["result"]["open_candidate_a1"]["candidate_ref"]
        historical_reviewer = "child-thread:a1-historical-open-reviewer"
        historical_binding = {
            "project_id": "project.rh",
            "mission_id": "mission.1",
            "executive_epoch_id": epoch_id,
            "root_thread_id": self.goal_thread_id,
            "actual_child_thread_id": historical_reviewer,
            "actual_parent_thread_id": self.goal_thread_id,
            "actual_depth": 1,
        }
        with (
            mock.patch.object(
                self.store,
                "list_mission_candidate_a1_bindings",
                side_effect=AssertionError("historical OPEN A1 enumerated Candidate history"),
            ),
            mock.patch(
                "research_core.workspace_store._validated_journal_index",
                side_effect=AssertionError("historical OPEN A1 built a full journal index"),
            ),
            mock.patch(
                "research_core.workspace_store._validated_transition_journal_chain",
                side_effect=AssertionError("historical OPEN A1 scanned the journal"),
            ),
        ):
            historical_grant = self.interface.issue_candidate_a1_review_grant(
                {
                    "child_thread_id": historical_reviewer,
                    "assignment": "Independently review exact Candidate revision one.",
                    "context": {
                        "id": "context:context.a1-historical-open-review",
                        "revision": 1,
                    },
                    "candidate_ref": {
                        "id": "candidate:candidate.direct-complete-rh-history",
                        "revision": first_ref["revision"],
                        "payload_sha256": first_ref["payload_sha256"],
                    },
                },
                binding=historical_binding,
            )
        self.assertEqual(historical_grant["candidate_ref"]["revision"], 1)
        reconstruction = self.interface.reconstruct()
        open_a1 = reconstruction["recovery_opening"]["open_candidate_a1"]
        self.assertEqual(
            [item["retrieval_handle"] for item in open_a1],
            [
                "candidate:candidate.direct-complete-rh-history@1",
                "candidate:candidate.direct-complete-rh-history@2",
            ],
        )
        candidate_delta = next(
            item
            for item in reconstruction["recovery_opening"]["owner_deltas"]
            if item["current_reference"]["kind"] == "candidate"
            and item["current_reference"]["identity"]
            == "candidate.direct-complete-rh-history"
        )
        self.assertEqual(
            candidate_delta["summary"]["candidate_a1"]["candidate_ref"]["revision"],
            2,
        )
        selected = self.interface.reconstruct(
            selected_readback_handles=[
                "candidate:candidate.direct-complete-rh-history@1",
                "candidate:candidate.direct-complete-rh-history@2",
            ]
        )["selected_readback"]
        self.assertEqual(
            [item["readback_kind"] for item in selected],
            ["open_candidate_a1_document", "current_owner_document"],
        )
        self.assertNotIn("gaps", selected[0]["document"])
        self.assertEqual(
            selected[1]["document"]["gaps"],
            ["Revision two adds an explicit reconstruction obligation."],
        )

        continued_only_on_current = self._execute(
            RECORD_STRATEGY,
            {
                "mission_continuation": "continue",
                "integrated_comparison": (
                    "The current purported proof revision is held for A1 review."
                ),
                "causal_inputs": [
                    {
                        "source": {
                            "id": revision_two["result"]["record_id"],
                        },
                        "decision_consequence": "Continue focused independent review.",
                    }
                ],
                "reconsideration_conditions": [
                    {
                        "condition": "Reconsider after independent A1 review.",
                        "owner_refs": [
                            {
                                "id": revision_two["result"]["record_id"],
                            }
                        ],
                    }
                ],
            },
            executive_epoch_id=epoch_id,
        )
        self.assertEqual(continued_only_on_current["status"], "completed")
        rejected = self._execute(
            CHECKPOINT,
            {},
            executive_epoch_id=epoch_id,
        )
        self.assertEqual(rejected["status"], "rejected")
        self.assertIn(
            "candidate:candidate.direct-complete-rh-history@1",
            rejected["error"]["message"],
        )
        self.store.verify_integrity()

    def test_migrated_candidate_v1_a1_is_recoverable_by_exact_revision(self) -> None:
        epoch_id = self._authorize()
        self._bind(epoch_id)
        metadata = self.store.read_metadata()
        authority_digest = str(metadata["canonical_authority_digest"])
        lease = self.store.reissue_writer_lease(attest_current_principal())
        candidate_id = "candidate.migrated-v1-a1"
        placeholder_digest = "1" * 64
        schema_path = (
            REPO_ROOT
            / "contracts"
            / "schemas"
            / "candidate_revision.v1.schema.json"
        )
        migrated_candidate = {
            "schema_version": 1,
            "kind": "candidate_revision",
            "contract_schema_sha256": hashlib.sha256(
                schema_path.read_bytes()
            ).hexdigest(),
            "candidate_id": candidate_id,
            "revision": 1,
            "previous_revision": None,
            "branch_id": "branch.theta",
            "producing_session_id": "session.migrated-v1",
            "producing_attempt_id": "attempt.migrated-v1",
            "author": {
                "principal_id": "principal.migrated-v1",
                "role": "candidate_author",
                "mission_id": "mission.1",
                "mission_bundle_digest": placeholder_digest,
                "session_id": "session.migrated-v1",
                "session_plan_digest": placeholder_digest,
                "attempt_id": "attempt.migrated-v1",
                "result_digest": placeholder_digest,
                "context_digest": placeholder_digest,
                "context_manifest_sha256": placeholder_digest,
                "evidence_closure_digest": placeholder_digest,
            },
            "lifecycle": "ready_for_independent_review",
            "exact_statement": (
                "Every nontrivial zero of the Riemann zeta function lies on "
                "the critical line."
            ),
            "claimed_scope": (
                "Complete unconditional proof of the Riemann Hypothesis."
            ),
            "domain": "analytic number theory",
            "objects": ["Riemann zeta function"],
            "hypotheses": [],
            "normalizations": ["standard completed zeta normalization"],
            "argument_steps": [
                {
                    "step_id": "step.migrated-v1",
                    "statement": "Purported complete legacy argument.",
                    "justification": "Preserved for independent reconstruction.",
                    "premise_refs": [],
                    "evidence_refs": [],
                }
            ],
            "dependencies": [],
            "component_evidence_refs": [
                {
                    "kind": "evidence",
                    "id": "evidence.migrated-v1-source",
                    "revision": 1,
                    "digest_sha256": placeholder_digest,
                }
            ],
            "objections": [],
            "gaps": [],
            "circularity_risks": [],
            "smallest_falsifier": "One nontrivial zero off the critical line.",
            "genealogy": [],
            "non_inferences": ["This Candidate is not admitted mathematics."],
            "full_rh_case": True,
            "evidence_closure": {
                "closure_id": "closure.migrated-v1-source",
                "closure_digest": placeholder_digest,
            },
            "created_at": "2026-08-24T00:00:00Z",
            "authority_class": "candidate_only",
            "canonical_effect": "none",
            "admission_state": "not_admitted",
        }
        migrated_digest = canonical_payload(migrated_candidate).sha256
        auxiliary_writes, evidence_advances = (
            self.store._prepare_successor_candidate_a1_projection(
                candidate_id=candidate_id,
                candidate_payload=migrated_candidate,
                candidate_digest=migrated_digest,
                actor="test.migrated-v1",
                expected_canonical_authority_digest=authority_digest,
            )
        )
        migrated_write = RevisionWrite(
            TypedWorkspaceId(IdentityKind.CANDIDATE, candidate_id),
            migrated_candidate,
            expected_revision=None,
        )
        # A current writer may only create Candidate-v2. Bypass only that
        # creation-time gate to materialize the exact post-migration retained
        # Candidate-v1 state; normal read and full integrity paths stay active.
        with mock.patch.object(
            self.store,
            "_validate_current_successor_candidate_a1",
            return_value=None,
        ):
            self.store._apply_successor_storage_command(
                family=RevisionCommandFamily.CANDIDATE,
                lease=lease,
                command_id="test.migrated-v1.commit",
                actor="test.migrated-v1",
                command_kind="commit_candidate_revision",
                executive_epoch_id=epoch_id,
                request={
                    "mission_id": "mission.1",
                    "candidate_id": candidate_id,
                    "migrated_fixture": True,
                },
                semantic_payload=migrated_candidate,
                writes=(migrated_write,),
                auxiliary_writes=auxiliary_writes,
                evidence_head_advances=evidence_advances,
                target_head_key=migrated_write.object_id.key,
                expected_target_revision=None,
                expected_target_payload_digest=None,
                dependency_heads=None,
                expected_canonical_authority_digest=authority_digest,
            )

        authority = self.interface._direct_epoch_authority(epoch_id)
        current_candidate = prepare_candidate_revision(
            authority=authority,
            candidate_id=candidate_id,
            proposal_kind="lemma",
            exact_statement=(
                "A later ordinary revision preserves the historical A1 only."
            ),
            standing={
                "status": "open",
                "basis": "Historical A1 remains separately preserved.",
            },
            scope_and_reach="one local lemma",
            repo_root=REPO_ROOT,
        )
        self.store.commit_candidate_revision(
            executive_epoch_id=epoch_id,
            mission_id="mission.1",
            candidate_id=candidate_id,
            payload=current_candidate.document,
            expected_head_revision=1,
            expected_head_payload_digest=migrated_digest,
            lease=lease,
            command_id="test.migrated-v1.advance",
            actor="test.migrated-v1",
            expected_canonical_authority_digest=authority_digest,
        )

        bindings = self.store.list_mission_candidate_a1_bindings("mission.1")
        self.assertEqual(
            [
                (item.candidate_id, item.candidate_revision, item.hold_lifecycle)
                for item in bindings
            ],
            [(candidate_id, 1, "open")],
        )
        reconstruction = self.interface.reconstruct()
        self.assertEqual(
            [
                item["retrieval_handle"]
                for item in reconstruction["recovery_opening"]["open_candidate_a1"]
            ],
            [f"candidate:{candidate_id}@1"],
        )
        self.assertNotIn(
            bindings[0].evidence_id,
            {
                item["current_reference"]["identity"]
                for item in reconstruction["recovery_opening"]["owner_deltas"]
            },
        )
        selected = self.interface.reconstruct(
            selected_readback_handles=[f"candidate:{candidate_id}@1"]
        )["selected_readback"]
        self.assertEqual(selected[0]["readback_kind"], "open_candidate_a1_document")
        self.assertEqual(selected[0]["document"], migrated_candidate)
        self.assertNotIn("mission_id", selected[0]["document"])
        self.assertEqual(selected[0]["document"]["author"]["mission_id"], "mission.1")
        self.store.verify_integrity()

    def test_failed_before_checkpoint_is_terminal_from_authorized(self) -> None:
        epoch_id = self._authorize()
        failed = self.interface.record_direct_failed_executive_epoch_from_owner(
            {
                "executiveEpochId": epoch_id,
                "reconciliation": {
                    "stage": "authorization_only",
                    "failure_reason": "Goal identity did not materialize.",
                },
            }
        )
        self.assertEqual(failed["state"], "failed_before_checkpoint")
        chain = self.store.read_executive_epoch_events(
            executive_epoch_id=epoch_id,
            mission_id="mission.1",
        )
        self.assertEqual(
            tuple(item["event_kind"] for item in chain),
            ("authorized", "failed_before_checkpoint"),
        )
        reconstruction = self.interface.reconstruct()
        self.assertEqual(
            reconstruction["latest_executive_epoch"],
            {
                "executive_epoch_id": epoch_id,
                "state": "failed_before_checkpoint",
                "goal_thread_id": None,
                "last_event_project_commit": chain[-1]["project_commit_no"],
                "checkpoint_ref": None,
                "reconciliation": {
                    "stage": "authorization_only",
                    "failure_reason": "Goal identity did not materialize.",
                },
            },
        )
        self._assert_checkpoint_rejected(epoch_id)

    def test_reconstruct_selected_readback_recovers_historical_epoch_failure(
        self,
    ) -> None:
        failed_epoch_id = self._authorize()
        self._bind(failed_epoch_id)
        failed = self.interface.record_direct_failed_executive_epoch_from_owner(
            {
                "executiveEpochId": failed_epoch_id,
                "reconciliation": {
                    "stage": "goal_runtime",
                    "failure_reason": "Goal ended without a checkpoint.",
                },
            }
        )
        self.assertEqual(failed["state"], "failed_before_checkpoint")
        failed_chain = self.store.read_executive_epoch_events(
            executive_epoch_id=failed_epoch_id,
            mission_id="mission.1",
        )
        successor_epoch_id = self._authorize()
        metadata_before = dict(self.store.read_metadata())

        handle = failed_epoch_id
        reconstruction = self.interface.reconstruct(
            selected_readback_handles=[handle]
        )

        self.assertEqual(
            reconstruction["latest_executive_epoch"]["executive_epoch_id"],
            successor_epoch_id,
        )
        self.assertEqual(
            reconstruction["selected_readback"],
            [
                {
                    "retrieval_handle": handle,
                    "readback_kind": "executive_epoch_summary",
                    "executive_epoch": {
                        "executive_epoch_id": failed_epoch_id,
                        "state": "failed_before_checkpoint",
                        "goal_thread_id": self.goal_thread_id,
                        "last_event_project_commit": failed_chain[-1][
                            "project_commit_no"
                        ],
                        "terminal_event_sha256": failed_chain[-1]["row_digest"],
                        "checkpoint_ref": None,
                        "reconciliation": {
                            "stage": "goal_runtime",
                            "failure_reason": "Goal ended without a checkpoint.",
                        },
                    },
                }
            ],
        )
        self.assertEqual(dict(self.store.read_metadata()), metadata_before)

        with self.assertRaisesRegex(
            MissionInterfaceError,
            "unversioned event-chain handle",
        ):
            self.interface.reconstruct(
                selected_readback_handles=[f"{handle}@1"]
            )
        with self.assertRaises(StaleCommandError):
            self.interface.reconstruct(
                selected_readback_handles=[f"epoch:{'f' * 64}"]
            )

    def test_empty_checkpoint_requires_epoch_work_then_accepts_strategy_continue(
        self,
    ) -> None:
        epoch_id = self._authorize()
        self._bind(epoch_id)
        metadata_before = dict(self.store.read_metadata())
        events_before = self.store.read_executive_epoch_events(
            executive_epoch_id=epoch_id,
            mission_id="mission.1",
        )

        rejected = self.interface.execute_semantic_operation(
            self._request(CHECKPOINT, {}),
            executive_epoch_id=epoch_id,
        )
        self.assertEqual(rejected["status"], "rejected", rejected)
        self.assertEqual(rejected["error"]["code"], "mission_checkpoint_rejected")
        self.assertEqual(
            rejected["error"]["correction"], "continue_epoch_before_checkpoint"
        )
        self.assertIn("terminal handoff", rejected["error"]["message"])
        self.assertEqual(metadata_before, dict(self.store.read_metadata()))
        self.assertEqual(
            events_before,
            self.store.read_executive_epoch_events(
                executive_epoch_id=epoch_id,
                mission_id="mission.1",
            ),
        )
        self.assertIsNone(
            self.store.read_continuation_checkpoint(executive_epoch_id=epoch_id)
        )
        self.assertEqual(self.interface.current_epoch()["state"], "open")

        continued = self._execute(
            RECORD_STRATEGY,
            {
                "mission_continuation": "continue",
                "integrated_comparison": (
                    "No durable research result is yet available; continue from a "
                    "specific next direction instead of treating an empty checkpoint as progress."
                ),
                "reconsideration_conditions": [
                    {
                        "condition": (
                            "Reconsider when the next materially grounded research "
                            "direction has been executed."
                        ),
                        "owner_refs": [],
                    }
                ],
            },
            executive_epoch_id=epoch_id,
        )
        self.assertEqual(continued["status"], "completed", continued)
        self.assertGreater(
            int(self.store.read_metadata()["current_project_commit"]),
            int(events_before[-1]["project_commit_no"]),
        )

        result = self.interface.execute_semantic_operation(
            self._request(CHECKPOINT, {}),
            executive_epoch_id=epoch_id,
        )
        self.assertEqual(result["status"], "completed", result)
        stored = self.store.read_continuation_checkpoint(
            executive_epoch_id=epoch_id
        )
        self.assertIsNotNone(stored)
        assert stored is not None
        self.assertEqual(
            self._checkpoint_sections(stored)["pending_capture_locators"],
            [],
        )
        self.assertEqual(self.interface.current_epoch()["state"], "closed")

    def test_complete_neutral_capture_annotation_removes_only_its_artifact_from_pending(self) -> None:
        epoch_id = self._authorize()
        self._bind(epoch_id)
        capture_id = self._capture_pending_material(epoch_id)

        annotated = self.interface.execute_semantic_operation(
            self._request(
                INTERPRET_MATERIAL,
                {
                    "judgment": "reviewed_no_current_semantic_delta",
                    "annotation_id": "capture-annotation:neutral-output",
                    "captured_material_id": f"capture:{capture_id}",
                    "artifact_ordinal": 0,
                    "exact_scope": "The complete captured output artifact.",
                    "coverage": "complete_artifact",
                    "lifecycle": "active",
                },
            ),
            executive_epoch_id=epoch_id,
        )
        self.assertEqual(annotated["status"], "completed", annotated)

        checkpointed = self.interface.execute_semantic_operation(
            self._request(CHECKPOINT, {}),
            executive_epoch_id=epoch_id,
        )
        self.assertEqual(checkpointed["status"], "completed", checkpointed)
        stored = self.store.read_continuation_checkpoint(
            executive_epoch_id=epoch_id
        )
        self.assertIsNotNone(stored)
        assert stored is not None
        self.assertEqual(
            self._checkpoint_sections(stored)["pending_capture_locators"],
            [],
        )

    def test_explicit_complete_evidence_coverage_clears_pending_without_magic_prose(self) -> None:
        epoch_id = self._authorize()
        self._bind(epoch_id)
        capture_id = self._capture_pending_material(epoch_id)
        strategy_before = read_mission_strategy_head(
            self.store, mission_id="mission.1"
        )
        self.assertEqual(
            strategy_before.record.document["mission_continuation"], "continue"
        )

        interpreted = self.interface.execute_semantic_operation(
            self._request(
                INTERPRET_MATERIAL,
                {
                    "evidence_id": "evidence:interpreted-output",
                    "capture_scopes": [
                        {
                            "captured_material_id": f"capture:{capture_id}",
                            "artifact_ordinal": 0,
                            "exact_scope": "Every byte in the worker output file.",
                            "coverage": "complete_artifact",
                        }
                    ],
                    "interpretation": (
                        "The output preserves one useful exact obstruction candidate."
                    ),
                    "scope": "The preserved worker output only.",
                    "strength": "heuristic",
                    "limitations": ["No general theorem follows yet."],
                    "significance": "The obstruction should shape the next branch.",
                },
            ),
            executive_epoch_id=epoch_id,
        )
        self.assertEqual(interpreted["status"], "completed", interpreted)

        checkpointed = self.interface.execute_semantic_operation(
            self._request(CHECKPOINT, {}),
            executive_epoch_id=epoch_id,
        )
        self.assertEqual(checkpointed["status"], "completed", checkpointed)
        strategy_after = read_mission_strategy_head(
            self.store, mission_id="mission.1"
        )
        self.assertEqual(strategy_after.revision, strategy_before.revision)
        self.assertEqual(
            strategy_after.record.payload_sha256,
            strategy_before.record.payload_sha256,
        )
        stored = self.store.read_continuation_checkpoint(
            executive_epoch_id=epoch_id
        )
        self.assertIsNotNone(stored)
        assert stored is not None
        self.assertEqual(
            self._checkpoint_sections(stored)["pending_capture_locators"],
            [],
        )

    def test_interpretation_explicitly_adopts_root_material_before_evidence(self) -> None:
        epoch_id = self._authorize()
        self._bind(epoch_id)
        exact_content = (
            "A source passage explicitly selected by the root executive for "
            "mathematical interpretation."
        )

        interpreted = self._execute(
            INTERPRET_MATERIAL,
            {
                "evidence_id": "evidence:root-adopted-source",
                "capture_scopes": [
                    {
                        "adopted_root_material": {
                            "channel": "web_search",
                            "content": exact_content,
                        },
                        "exact_scope": "The complete adopted source passage.",
                        "coverage": "complete_artifact",
                    }
                ],
                "interpretation": (
                    "The adopted passage states one source claim worth testing."
                ),
                "scope": "The exact adopted passage only.",
                "strength": "source",
                "limitations": ["The source claim has not yet been proved."],
                "significance": "The claim can now be tested against the frontier.",
            },
            executive_epoch_id=epoch_id,
        )
        self.assertEqual(interpreted["status"], "completed", interpreted)

        evidence = read_evidence_meaning(
            self.store,
            evidence_id="root-adopted-source",
        )
        self.assertEqual(len(evidence.sources), 1)
        capture_id = evidence.sources[0].capture_id
        capture = read_raw_capture(self.store, capture_id=capture_id)
        self.assertEqual(capture.executive_epoch_id, epoch_id)
        self.assertEqual(capture.assignment_id, self.goal_thread_id)
        self.assertEqual(capture.capture_kind, "output")
        self.assertEqual(
            capture.provenance,
            {
                "bridge": "root_executive_interpret_material",
                "root_thread_id": self.goal_thread_id,
                "declared_channel": "web_search",
            },
        )
        artifact = read_raw_capture_artifact(
            self.store,
            cas=self.cas,
            mission_id="mission.1",
            capture_id=capture_id,
            artifact_ordinal=0,
        )
        self.assertEqual(artifact.content_bytes, exact_content.encode("utf-8"))
        self.assertEqual(self.interface._pending_capture_locators(), ())

    def test_adopted_native_assignment_recovery_preserves_exact_lineage(self) -> None:
        epoch_id = self._authorize()
        self._bind(epoch_id)
        content = "Investigate the exact local obstruction in the delegated branch."
        parent_thread_id = "thread:researcher:assignment-parent"
        child_thread_id = "thread:researcher:assignment-child"
        lineage = {
            "material_kind": "assignment",
            "parent_thread_id": parent_thread_id,
            "child_thread_id": child_thread_id,
        }
        semantic_input = {
            "evidence_id": "evidence:recovered-native-assignment",
            "capture_scopes": [
                {
                    "adopted_root_material": {
                        "channel": "native_assignment",
                        "content": content,
                        "native_lineage": lineage,
                    },
                    "exact_scope": "The complete delegated assignment.",
                    "coverage": "complete_artifact",
                }
            ],
            "interpretation": "The worker received this exact bounded assignment.",
            "scope": "The exact recovered native assignment only.",
            "strength": "source",
            "limitations": ["No worker output is inferred from its assignment."],
            "significance": "The assignment lineage is available for recovery.",
        }

        wrong_channel = json.loads(json.dumps(semantic_input))
        wrong_channel["capture_scopes"][0]["adopted_root_material"][
            "channel"
        ] = "native_output"
        rejected = self._execute(
            INTERPRET_MATERIAL,
            wrong_channel,
            executive_epoch_id=epoch_id,
        )
        self.assertEqual(rejected["status"], "rejected", rejected)
        self.assertEqual(self.store.list_mission_raw_captures("mission.1"), ())

        interpreted = self._execute(
            INTERPRET_MATERIAL,
            semantic_input,
            executive_epoch_id=epoch_id,
        )
        self.assertEqual(interpreted["status"], "completed", interpreted)
        expected_observation_id = hashlib.sha256(
            canonical_json_bytes(
                {
                    "materialKind": "assignment",
                    "content": content,
                    "rootThreadId": self.goal_thread_id,
                    "parentThreadId": parent_thread_id,
                    "childThreadId": child_thread_id,
                }
            )
        ).hexdigest()
        evidence = read_evidence_meaning(
            self.store,
            evidence_id="recovered-native-assignment",
        )
        self.assertEqual(len(evidence.sources), 1)
        capture_id = evidence.sources[0].capture_id
        capture = read_raw_capture(self.store, capture_id=capture_id)
        self.assertEqual(capture.capture_kind, "assignment")
        self.assertEqual(capture.observation_id, expected_observation_id)
        self.assertEqual(capture.assignment_id, child_thread_id)
        self.assertEqual(
            capture.provenance,
            {
                "bridge": "native_host_observation",
                "observation_id": expected_observation_id,
                "material_kind": "assignment",
                "root_thread_id": self.goal_thread_id,
                "parent_thread_id": parent_thread_id,
                "child_thread_id": child_thread_id,
            },
        )
        self.assertEqual(capture.artifacts[0].role, "native_assignment")
        self.assertEqual(
            capture.artifacts[0].logical_name,
            f"{expected_observation_id}.assignment.txt",
        )

        replay = self.interface.capture_native_material_observation(
            {
                "observationId": expected_observation_id,
                "materialKind": "assignment",
                "content": content,
                "rootThreadId": self.goal_thread_id,
                "parentThreadId": parent_thread_id,
                "childThreadId": child_thread_id,
            },
            executive_epoch_id=epoch_id,
        )
        self.assertEqual(replay["material_id"], f"capture:{capture_id}")
        self.assertEqual(len(self.store.list_mission_raw_captures("mission.1")), 1)

        retrieved = self._execute(
            RETRIEVE,
            {
                "mode": "read",
                "purpose": "Read the exact recovered assignment lineage.",
                "ids": [f"capture:{capture_id}"],
            },
            executive_epoch_id=epoch_id,
        )
        readable = json.loads(retrieved["result"]["items"][0]["readable_content"])
        self.assertEqual(readable["capture_kind"], "assignment")
        self.assertEqual(readable["observation_id"], expected_observation_id)
        self.assertEqual(readable["assignment_id"], child_thread_id)
        self.assertEqual(
            readable["native_lineage"],
            {
                "parent_thread_id": parent_thread_id,
                "child_thread_id": child_thread_id,
            },
        )
        descriptor = next(
            item
            for item in self.interface.reconstruct()["recovery_opening"][
                "raw_captures"
            ]
            if item["capture_id"] == capture_id
        )
        self.assertEqual(descriptor["capture_kind"], "assignment")
        self.assertEqual(descriptor["observation_id"], expected_observation_id)
        self.assertEqual(descriptor["assignment_id"], child_thread_id)
        self.assertEqual(descriptor["native_lineage"], readable["native_lineage"])

    def test_adopted_identical_native_outputs_from_distinct_children_do_not_collapse(
        self,
    ) -> None:
        epoch_id = self._authorize()
        self._bind(epoch_id)
        content = "Both workers returned this byte-identical mathematical result."
        children = (
            "thread:identical-output:first",
            "thread:identical-output:second",
        )
        interpreted = self._execute(
            INTERPRET_MATERIAL,
            {
                "evidence_id": "evidence:distinct-identical-native-outputs",
                "capture_scopes": [
                    {
                        "adopted_root_material": {
                            "channel": "native_output",
                            "content": content,
                            "native_lineage": {
                                "material_kind": "output",
                                "parent_thread_id": self.goal_thread_id,
                                "child_thread_id": child_thread_id,
                            },
                        },
                        "exact_scope": f"The complete output from {child_thread_id}.",
                        "coverage": "complete_artifact",
                    }
                    for child_thread_id in children
                ],
                "interpretation": (
                    "Two distinct delegated children independently returned the same text."
                ),
                "scope": "The two exact native outputs and their separate lineages.",
                "strength": "source",
                "limitations": ["Text equality does not identify the assignments."],
                "significance": "Both child results remain separately attributable.",
            },
            executive_epoch_id=epoch_id,
        )
        self.assertEqual(interpreted["status"], "completed", interpreted)
        evidence = read_evidence_meaning(
            self.store,
            evidence_id="distinct-identical-native-outputs",
        )
        capture_ids = {source.capture_id for source in evidence.sources}
        self.assertEqual(len(capture_ids), 2)
        captures = [
            read_raw_capture(self.store, capture_id=capture_id)
            for capture_id in sorted(capture_ids)
        ]
        self.assertEqual({item.assignment_id for item in captures}, set(children))
        self.assertEqual({item.capture_kind for item in captures}, {"output"})
        self.assertEqual(len({item.observation_id for item in captures}), 2)
        for capture in captures:
            artifact = read_raw_capture_artifact(
                self.store,
                cas=self.cas,
                mission_id="mission.1",
                capture_id=capture.capture_id,
                artifact_ordinal=0,
            )
            self.assertEqual(artifact.content_bytes, content.encode("utf-8"))

        descriptors = {
            item["capture_id"]: item
            for item in self.interface.reconstruct()["recovery_opening"][
                "raw_captures"
            ]
            if item["capture_id"] in capture_ids
        }
        self.assertEqual(set(descriptors), capture_ids)
        self.assertEqual(
            {item["assignment_id"] for item in descriptors.values()},
            set(children),
        )

    def test_adopted_root_capture_replays_after_evidence_commit_failure(self) -> None:
        epoch_id = self._authorize()
        self._bind(epoch_id)
        semantic_input = {
            "evidence_id": "evidence:root-adoption-retry",
            "capture_scopes": [
                {
                    "adopted_root_material": {
                        "channel": "shell",
                        "content": "Exact deterministic computation output.",
                    },
                    "exact_scope": "The complete deterministic output.",
                    "coverage": "complete_artifact",
                }
            ],
            "interpretation": "The computation gives one bounded diagnostic.",
            "scope": "The exact adopted shell output only.",
            "strength": "numerical_diagnostic",
            "limitations": ["This is not a proof of the general case."],
            "significance": "The diagnostic changes the next finite test.",
        }
        with mock.patch(
            "research_core.mission_interface.commit_evidence_meaning",
            side_effect=ValueError("simulated Evidence commit rejection"),
        ):
            failed = self._execute(
                INTERPRET_MATERIAL,
                semantic_input,
                executive_epoch_id=epoch_id,
            )
        self.assertEqual(failed["status"], "rejected", failed)
        first_captures = self.store.list_mission_raw_captures("mission.1")
        self.assertEqual(len(first_captures), 1)

        retried = self._execute(
            INTERPRET_MATERIAL,
            semantic_input,
            executive_epoch_id=epoch_id,
        )
        self.assertEqual(retried["status"], "completed", retried)
        self.assertEqual(
            self.store.list_mission_raw_captures("mission.1"),
            first_captures,
        )

    def test_invalid_existing_scope_preflights_before_root_adoption(self) -> None:
        epoch_id = self._authorize()
        self._bind(epoch_id)
        rejected = self._execute(
            INTERPRET_MATERIAL,
            {
                "evidence_id": "evidence:mixed-preflight",
                "capture_scopes": [
                    {
                        "adopted_root_material": {
                            "channel": "web_search",
                            "content": "This must not be captured yet.",
                        },
                        "exact_scope": "The complete root passage.",
                        "coverage": "complete_artifact",
                    },
                    {
                        "captured_material_id": "capture:missing-capture",
                        "artifact_ordinal": 0,
                        "exact_scope": "The absent capture.",
                        "coverage": "complete_artifact",
                    },
                ],
                "interpretation": "The invalid mixed request has no meaning.",
                "scope": "No material.",
                "strength": "heuristic",
                "limitations": [],
                "significance": "No state should change.",
            },
            executive_epoch_id=epoch_id,
        )
        self.assertEqual(rejected["status"], "rejected", rejected)
        self.assertEqual(self.store.list_mission_raw_captures("mission.1"), ())
        self.assertEqual(
            self.store.list_mission_evidence_meaning_heads("mission.1"), ()
        )

    def test_failed_before_checkpoint_is_terminal_from_bound(self) -> None:
        epoch_id = self._authorize()
        self._bind(epoch_id)
        failed = self.interface.record_direct_failed_executive_epoch_from_owner(
            {
                "executiveEpochId": epoch_id,
                "reconciliation": {
                    "stage": "goal_runtime",
                    "failure_reason": "Goal ended without a checkpoint.",
                },
            }
        )
        self.assertEqual(failed["state"], "failed_before_checkpoint")
        chain = self.store.read_executive_epoch_events(
            executive_epoch_id=epoch_id,
            mission_id="mission.1",
        )
        self.assertEqual(
            tuple(item["event_kind"] for item in chain),
            ("authorized", "bound", "failed_before_checkpoint"),
        )
        self._assert_checkpoint_rejected(epoch_id)

    def test_failed_before_checkpoint_terminalizes_the_historical_root_after_fence(self) -> None:
        epoch_id = self._authorize()
        self._bind(epoch_id)
        self.interface.fence_mission_from_owner(
            {
                "threadId": self.goal_thread_id,
                "reason": "shared_authority_loss",
                "containmentScope": "mission_fence",
            },
            executive_epoch_id=epoch_id,
        )
        failed = self.interface.record_direct_failed_executive_epoch_from_owner(
            {
                "executiveEpochId": epoch_id,
                "reconciliation": {
                    "stage": "goal_runtime",
                    "failure_reason": "Mission-wide effect required fencing.",
                },
            }
        )
        self.assertEqual(
            failed,
            {
                "executive_epoch_id": epoch_id,
                "state": "failed_before_checkpoint",
            },
        )
        chain = self.store.read_executive_epoch_events(
            executive_epoch_id=epoch_id,
            mission_id="mission.1",
        )
        self.assertEqual(
            tuple(item["event_kind"] for item in chain),
            ("authorized", "bound", "failed_before_checkpoint"),
        )

    def test_reconciled_failure_can_still_fence_the_bound_mission(self) -> None:
        epoch_id = self._authorize()
        self._bind(epoch_id)
        before = self.store.get_head(
            TypedWorkspaceId(IdentityKind.MISSION, "mission.1")
        )
        self.assertIsNotNone(before)
        failed = self.interface.record_direct_failed_executive_epoch_from_owner(
            {
                "executiveEpochId": epoch_id,
                "reconciliation": {
                    "stage": "goal_runtime",
                    "failure_reason": "Goal effect required Mission containment.",
                },
            }
        )
        self.assertEqual(failed["state"], "failed_before_checkpoint")
        fenced = self.interface.fence_mission_from_owner(
            {
                "threadId": self.goal_thread_id,
                "reason": "unknown_effect",
                "containmentScope": "mission_fence",
            },
            executive_epoch_id=epoch_id,
        )
        self.assertEqual(fenced, {"mission_id": "mission.1", "state": "fenced"})
        after = self.store.get_head(
            TypedWorkspaceId(IdentityKind.MISSION, "mission.1")
        )
        self.assertIsNotNone(after)
        assert before is not None and after is not None
        self.assertEqual(after.reference.revision, before.reference.revision + 1)
        self.assertEqual(after.payload["fence_reason"], "revoked")
        chain = self.store.read_executive_epoch_events(
            executive_epoch_id=epoch_id,
            mission_id="mission.1",
        )
        self.assertEqual(
            tuple(item["event_kind"] for item in chain),
            ("authorized", "bound", "failed_before_checkpoint"),
        )

    def test_mission_fence_requires_the_durable_goal_thread(self) -> None:
        epoch_id = self._authorize()
        self._bind(epoch_id)
        before = self.store.get_head(
            TypedWorkspaceId(IdentityKind.MISSION, "mission.1")
        )
        self.assertIsNotNone(before)
        with self.assertRaises(StaleCommandError) as rejected:
            self.interface.fence_mission_from_owner(
                {
                    "threadId": "goal-thread:another-runtime",
                    "reason": "shared_authority_loss",
                    "containmentScope": "mission_fence",
                },
                executive_epoch_id=epoch_id,
            )
        self.assertEqual(
            rejected.exception.diagnostic_code,
            "mission_fence_owner_epoch_binding_stale",
        )
        after = self.store.get_head(
            TypedWorkspaceId(IdentityKind.MISSION, "mission.1")
        )
        self.assertEqual(after, before)

    def test_mission_fence_stale_root_and_conflict_keep_original_rejections(self) -> None:
        epoch_id = self._authorize()
        self._bind(epoch_id)
        before = self.store.get_head(TypedWorkspaceId(IdentityKind.MISSION, "mission.1"))
        assert before is not None
        arguments = {
            "executive_epoch_id": epoch_id,
            "mission_id": "mission.1",
            "payload": direct_mission_fence_successor(
                before.payload, fenced_at="2026-09-04T00:00:00Z"
            ),
            "expected_head_revision": before.reference.revision,
            "expected_head_payload_digest": before.payload_digest,
            "lease": self.interface._writer_lease(),
            "command_id": "test.exact-fence",
            "actor": "test-fence-owner",
            "expected_canonical_authority_digest": str(
                self.store.read_metadata()["canonical_authority_digest"]
            ),
        }
        self.store.fence_mission_revision(**arguments)
        contained = self.store.read_metadata()
        fenced_head = self.store.get_head(before.reference.object_id)
        with self.assertRaises(MissionFenceStaleCommandError) as stale:
            self.store.fence_mission_revision(
                **{**arguments, "command_id": "test.other-fence"}
            )
        self.assertIsInstance(stale.exception, StaleCommandError)
        self.assertEqual(
            stale.exception.diagnostic_code,
            "mission_fence_store_authorized_root_stale",
        )
        with self.assertRaises(CommandConflictError):
            self.store.fence_mission_revision(
                **{
                    **arguments,
                    "payload": direct_mission_fence_successor(
                        before.payload, fenced_at="2026-09-04T00:00:01Z"
                    ),
                }
            )
        self.assertEqual(self.store.read_metadata(), contained)
        self.assertEqual(self.store.get_head(before.reference.object_id), fenced_head)
        self.assertEqual(self.interface.current_epoch()["state"], "mission_fenced")
        self.assertEqual(
            tuple(item["event_kind"] for item in self.store.read_executive_epoch_events(
                executive_epoch_id=epoch_id, mission_id="mission.1"
            )),
            ("authorized", "bound"),
        )

    def test_mission_fence_diagnostics_follow_real_owner_writer_store_boundaries(self) -> None:
        import test_rh_mission_cli as cli

        epoch_id = self._authorize()
        self._bind(epoch_id)
        request = cli._bridge_request("fence_mission", {
            "context": {
                "threadId": self.goal_thread_id,
                "reason": "mission_consistency_failure",
                "containmentScope": "mission_fence",
            },
            "binding": {"executiveEpochId": epoch_id},
        })
        before = self.store.read_metadata()
        cases = (
            (
                self.interface, "_writer_lease", StaleWriterError("private writer detail"),
                "workspace_error", "writer", "stale_writer",
            ),
            (
                self.store, "fence_mission_revision", ValueError("private Store detail"),
                "invalid_invocation", "store", "value",
            ),
        )
        for owner, method, error, primary, stage, category in cases:
            with self.subTest(stage=stage), mock.patch.object(owner, method, side_effect=error):
                exit_code, result = cli.RhMissionCliTests()._run_bridge(request, self.interface)
                self.assertEqual(exit_code, 2, result)
                self.assertEqual([item["code"] for item in result["errors"]], [
                    primary, f"mission_fence_stage_{stage}", f"mission_fence_error_{category}",
                ])
                self.assertIsInstance(error, (StaleWriterError, ValueError))
            self.assertEqual(self.store.read_metadata(), before)
            self.assertEqual(self.interface.current_epoch()["state"], "open")


if __name__ == "__main__":
    unittest.main()
