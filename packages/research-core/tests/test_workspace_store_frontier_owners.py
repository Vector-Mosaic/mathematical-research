from __future__ import annotations

import hashlib
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


PACKAGE_ROOT = Path(__file__).resolve().parents[1]
TEST_ROOT = Path(__file__).resolve().parent
REPO_ROOT = Path(__file__).resolve().parents[3]

for candidate in (PACKAGE_ROOT, TEST_ROOT):
    if str(candidate) not in sys.path:
        sys.path.insert(0, str(candidate))

from research_core.canonical_snapshot import load_canonical_snapshot  # noqa: E402
from research_core.candidate_revision import (  # noqa: E402
    candidate_a1_requirement,
    candidate_schema_digest,
    candidate_schema_digest_v3,
    candidate_schema_digest_v4,
)
from research_core.evidence_store import EvidenceCAS, EvidenceItemRevision  # noqa: E402
from research_core.json_support import canonical_json_bytes  # noqa: E402
from research_core.mission_executive import (  # noqa: E402
    OwnerRevisionRef,
    prepare_direct_executive_epoch_authorized_event,
    prepare_direct_executive_epoch_bound_event,
    reissue_direct_executive_epoch_authority,
)
from research_core.mission_interface import MissionInterface  # noqa: E402
from research_core.workspace_paths import WorkspacePaths  # noqa: E402
from research_core.workspace_schema import (  # noqa: E402
    IdentityKind,
    TypedWorkspaceId,
    canonical_payload,
)
from research_core.workspace_store import (  # noqa: E402
    CommandConflictError,
    RevisionCommandFamily,
    RevisionWrite,
    StaleCommandError,
    WorkspaceIntegrityError,
    WorkspaceStore,
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


class SuccessorFrontierOwnerStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        loaded = load_canonical_snapshot(REPO_ROOT, source_commit="a" * 40)
        self.assertTrue(loaded.ok, loaded.failure)
        assert loaded.value is not None
        self.canonical_snapshot = loaded.value
        self.store, self.authority_digest, self.lease = self._new_store("workspace")

    def _new_store(self, suffix: str):
        store = WorkspaceStore.initialize_direct_mission_workspace(
            WorkspacePaths.from_root(Path(self.temporary.name) / suffix),
            project_id="project.rh",
            canonical_snapshot=self.canonical_snapshot,
            actor="frontier-owner-test",
        )
        metadata = store.read_metadata()
        authority_digest = str(metadata["canonical_authority_digest"])
        lease = store.claim_writer(
            owner=f"frontier-owner-test:{suffix}",
            creation_basis="focused-root5-frontier-owner-test",
            expected_project_commit=int(metadata["current_project_commit"]),
            expected_root_digest=str(metadata["current_root_digest"]),
            expected_canonical_authority_digest=authority_digest,
        )
        store.initialize_direct_mission_genesis(
            _direct_genesis_fixture(),
            mission_id="mission.1",
            canonical_snapshot=self.canonical_snapshot,
            lease=lease,
            command_id=f"frontier-owner-test.{suffix}.direct-genesis",
            actor="frontier-owner-test",
        )
        mission = store.get_head(
            TypedWorkspaceId(IdentityKind.MISSION, "mission.1")
        )
        self.assertIsNotNone(mission)
        assert mission is not None
        self.executive_epoch_id = f"epoch.frontier-owner.{suffix}"
        authorized = prepare_direct_executive_epoch_authorized_event(
            executive_epoch_id=self.executive_epoch_id,
            mission_root=OwnerRevisionRef(
                "mission",
                "mission.1",
                mission.reference.revision,
                mission.payload_digest,
            ),
            predecessor_checkpoint=None,
        )
        authorization_metadata = store.read_metadata()
        store.authorize_executive_epoch(
            mission_id="mission.1",
            executive_epoch_id=self.executive_epoch_id,
            event=authorized,
            lease=lease,
            command_id=f"{self.executive_epoch_id}.authorize",
            actor="frontier-owner-test",
            expected_canonical_authority_digest=authority_digest,
            expected_mission_host_store_cut={
                "project_commit": int(authorization_metadata["current_project_commit"]),
                "current_root_digest": str(authorization_metadata["current_root_digest"]),
                "transition_head_digest": authorization_metadata["transition_head_digest"],
                "canonical_authority_digest": str(
                    authorization_metadata["canonical_authority_digest"]
                ),
            },
        )
        bound = prepare_direct_executive_epoch_bound_event(
            executive_epoch_id=self.executive_epoch_id,
            mission_id="mission.1",
            goal_thread_id=f"goal-thread:{self.executive_epoch_id}",
            workspace_root=str(store.paths.root),
        )
        store.bind_executive_epoch(
            mission_id="mission.1",
            executive_epoch_id=self.executive_epoch_id,
            event=bound,
            expected_event_ordinal=1,
            expected_event_digest=authorized.digest_sha256,
            lease=lease,
            command_id=f"{self.executive_epoch_id}.bind",
            actor="frontier-owner-test",
            expected_canonical_authority_digest=authority_digest,
        )
        active_epoch = store.read_active_executive_epoch("mission.1")
        self.assertIsNotNone(active_epoch)
        assert active_epoch is not None
        self.executive_epoch_authority_sha256 = (
            reissue_direct_executive_epoch_authority(
                event_readbacks=active_epoch,
                project_id="project.rh",
                root_identity=store.paths.root_identity,
                canonical_authority_digest=authority_digest,
            ).authority_sha256
        )
        return store, authority_digest, lease

    def _commit_candidate(
        self,
        *,
        candidate_id: str,
        mission_id: str = "mission.1",
        meaning: str,
        command_id: str,
        expected_revision: int | None = None,
        expected_digest: str | None = None,
        dependencies: dict[str, tuple[int, str]] | None = None,
        a1_requirement: dict[str, object] | None = None,
    ):
        return self.store.commit_candidate_revision(
            executive_epoch_id=self.executive_epoch_id,
            mission_id=mission_id,
            candidate_id=candidate_id,
            payload={
                "schema_version": 4,
                "kind": "candidate_revision",
                "contract_schema_sha256": candidate_schema_digest_v4(REPO_ROOT),
                "candidate_id": candidate_id,
                "mission_id": mission_id,
                "proposal_kind": "mathematical_statement",
                "exact_statement": meaning,
                "standing": {
                    "status": "open",
                    "basis": "focused frontier-owner Store fixture",
                },
                "provenance": {
                    "kind": "native_executive_epoch",
                    "executive_epoch_id": self.executive_epoch_id,
                    "executive_epoch_authority_sha256": (
                        self.executive_epoch_authority_sha256
                    ),
                },
                "authority_class": "candidate_only",
                "canonical_effect": "none",
            },
            expected_head_revision=expected_revision,
            expected_head_payload_digest=expected_digest,
            dependency_heads=dependencies,
            a1_requirement=a1_requirement,
            lease=self.lease,
            command_id=command_id,
            actor="frontier-owner-test",
            expected_canonical_authority_digest=self.authority_digest,
        )

    def _commit_branch(
        self,
        *,
        branch_id: str,
        mission_id: str = "mission.1",
        meaning: str,
        command_id: str,
        expected_revision: int | None = None,
        expected_digest: str | None = None,
        dependencies: dict[str, tuple[int, str]] | None = None,
    ):
        return self.store.commit_branch_revision(
            executive_epoch_id=self.executive_epoch_id,
            mission_id=mission_id,
            branch_id=branch_id,
            payload={
                "branch_id": branch_id,
                "mission_id": mission_id,
                "meaning": meaning,
            },
            expected_head_revision=expected_revision,
            expected_head_payload_digest=expected_digest,
            dependency_heads=dependencies,
            lease=self.lease,
            command_id=command_id,
            actor="frontier-owner-test",
            expected_canonical_authority_digest=self.authority_digest,
        )

    def _commit_strategy(
        self,
        *,
        strategy_id: str,
        mission_id: str = "mission.1",
        meaning: str,
        command_id: str,
        expected_revision: int | None = None,
        expected_digest: str | None = None,
        dependencies: dict[str, tuple[int, str]] | None = None,
    ):
        return self.store.commit_strategy_revision(
            executive_epoch_id=self.executive_epoch_id,
            mission_id=mission_id,
            strategy_id=strategy_id,
            payload={
                "strategy_id": strategy_id,
                "mission_id": mission_id,
                "meaning": meaning,
            },
            expected_head_revision=expected_revision,
            expected_head_payload_digest=expected_digest,
            dependency_heads=dependencies,
            lease=self.lease,
            command_id=command_id,
            actor="frontier-owner-test",
            expected_canonical_authority_digest=self.authority_digest,
        )

    def test_replay_noop_and_staleness_are_owner_local(self) -> None:
        created = self._commit_candidate(
            candidate_id="candidate.alpha",
            meaning="first exact candidate meaning",
            command_id="candidate.alpha.create",
        )
        replay = self._commit_candidate(
            candidate_id="candidate.alpha",
            meaning="first exact candidate meaning",
            command_id="candidate.alpha.create",
        )
        self.assertFalse(created.replayed)
        self.assertTrue(replay.replayed)
        with self.assertRaises(CommandConflictError):
            self._commit_candidate(
                candidate_id="candidate.alpha",
                meaning="changed command envelope",
                command_id="candidate.alpha.create",
            )

        first = self.store.read_candidate_revision(
            mission_id="mission.1",
            candidate_id="candidate.alpha",
        )
        metadata_before = self.store.read_metadata()
        with self.store.snapshot_connection() as connection:
            journal_before = int(
                connection.execute(
                    "SELECT COUNT(*) FROM transition_journal"
                ).fetchone()[0]
            )
        no_op = self._commit_candidate(
            candidate_id="candidate.alpha",
            meaning="first exact candidate meaning",
            command_id="candidate.alpha.noop",
            expected_revision=int(first["revision"]),
            expected_digest=str(first["payload_digest"]),
        )
        self.assertTrue(no_op.replayed)
        self.assertTrue(no_op.result["semantic_noop"])
        self.assertEqual(
            self.store.read_metadata()["current_project_commit"],
            metadata_before["current_project_commit"],
        )
        with self.store.snapshot_connection() as connection:
            self.assertEqual(
                int(
                    connection.execute(
                        "SELECT COUNT(*) FROM transition_journal"
                    ).fetchone()[0]
                ),
                journal_before,
            )
            self.assertIsNone(
                connection.execute(
                    "SELECT 1 FROM command_result WHERE command_id = ?",
                    ("candidate.alpha.noop",),
                ).fetchone()
            )

        current_strategy = self.store.read_mission_strategy_head("mission.1")
        self._commit_strategy(
            strategy_id="strategy.theta.1",
            meaning="unrelated Strategy owner",
            command_id="strategy.unrelated-update",
            expected_revision=int(current_strategy["revision"]),
            expected_digest=str(current_strategy["payload_digest"]),
        )
        self._commit_candidate(
            candidate_id="candidate.alpha",
            meaning="candidate survives unrelated owner advancement",
            command_id="candidate.alpha.update",
            expected_revision=int(first["revision"]),
            expected_digest=str(first["payload_digest"]),
        )
        second = self.store.read_candidate_revision(
            mission_id="mission.1",
            candidate_id="candidate.alpha",
        )
        late_replay = self._commit_candidate(
            candidate_id="candidate.alpha",
            meaning="first exact candidate meaning",
            command_id="candidate.alpha.create",
        )
        self.assertTrue(late_replay.replayed)
        self.assertEqual(late_replay.project_commit, created.project_commit)
        with self.assertRaisesRegex(StaleCommandError, "target head"):
            self._commit_candidate(
                candidate_id="candidate.alpha",
                meaning="stale target must not overwrite",
                command_id="candidate.alpha.stale-target",
                expected_revision=int(first["revision"]),
                expected_digest=str(first["payload_digest"]),
            )

        self._commit_branch(
            branch_id="branch.dependency",
            meaning="dependency revision one",
            command_id="branch.dependency.create",
        )
        dependency = self.store.read_branch_revision(
            mission_id="mission.1",
            branch_id="branch.dependency",
        )
        self._commit_branch(
            branch_id="branch.dependency",
            meaning="dependency revision two",
            command_id="branch.dependency.update",
            expected_revision=int(dependency["revision"]),
            expected_digest=str(dependency["payload_digest"]),
        )
        with self.assertRaisesRegex(StaleCommandError, "dependency head"):
            self._commit_candidate(
                candidate_id="candidate.alpha",
                meaning="stale dependency must block only this dependent write",
                command_id="candidate.alpha.stale-dependency",
                expected_revision=int(second["revision"]),
                expected_digest=str(second["payload_digest"]),
                dependencies={
                    "branch:branch.dependency": (
                        int(dependency["revision"]),
                        str(dependency["payload_digest"]),
                    )
                },
            )

    def test_mission_enumeration_history_and_exactly_one_strategy(self) -> None:
        for candidate_id in ("candidate.alpha", "candidate.beta"):
            self._commit_candidate(
                candidate_id=candidate_id,
                meaning=f"meaning for {candidate_id}",
                command_id=f"{candidate_id}.create",
            )
        with self.assertRaisesRegex(StaleCommandError, "another Mission"):
            self._commit_candidate(
                candidate_id="candidate.foreign",
                mission_id="mission.2",
                meaning="foreign Mission Candidate",
                command_id="candidate.foreign.create",
            )
        candidate_heads = self.store.list_mission_candidate_heads("mission.1")
        self.assertEqual(
            [item["candidate_id"] for item in candidate_heads],
            ["candidate.alpha", "candidate.beta"],
        )

        alpha_one = self.store.read_candidate_revision(
            mission_id="mission.1",
            candidate_id="candidate.alpha",
        )
        self._commit_candidate(
            candidate_id="candidate.alpha",
            meaning="second alpha meaning",
            command_id="candidate.alpha.update",
            expected_revision=int(alpha_one["revision"]),
            expected_digest=str(alpha_one["payload_digest"]),
        )
        alpha_history = self.store.read_candidate_revision(
            mission_id="mission.1",
            candidate_id="candidate.alpha",
            revision=1,
        )
        self.assertEqual(
            alpha_history["payload"]["exact_statement"],
            "meaning for candidate.alpha",
        )
        self.assertEqual(
            self.store.read_candidate_revision(
                mission_id="mission.1",
                candidate_id="candidate.alpha",
            )["revision"],
            2,
        )
        with self.assertRaises(StaleCommandError):
            self.store.read_candidate_revision(
                mission_id="mission.2",
                candidate_id="candidate.alpha",
            )

        for branch_id in ("branch.alpha", "branch.beta"):
            self._commit_branch(
                branch_id=branch_id,
                meaning=f"meaning for {branch_id}",
                command_id=f"{branch_id}.create",
            )
        with self.assertRaisesRegex(StaleCommandError, "another Mission"):
            self._commit_branch(
                branch_id="branch.foreign",
                mission_id="mission.2",
                meaning="foreign Mission Branch",
                command_id="branch.foreign.create",
            )
        self.assertEqual(
            [
                item["branch_id"]
                for item in self.store.list_mission_branch_heads("mission.1")
            ],
            ["branch.alpha", "branch.beta", "branch.theta"],
        )

        opening_strategy = self.store.read_mission_strategy_head("mission.1")
        self.assertEqual(opening_strategy["strategy_id"], "strategy.theta.1")
        self._commit_strategy(
            strategy_id="strategy.theta.1",
            meaning="one coherent Mission-wide comparison",
            command_id="strategy.theta.update",
            expected_revision=int(opening_strategy["revision"]),
            expected_digest=str(opening_strategy["payload_digest"]),
        )
        self.assertEqual(
            self.store.read_mission_strategy_head("mission.1")["strategy_id"],
            "strategy.theta.1",
        )
        with self.assertRaisesRegex(StaleCommandError, "another Mission"):
            self._commit_strategy(
                strategy_id="strategy.foreign",
                mission_id="mission.2",
                meaning="foreign Mission strategy",
                command_id="strategy.foreign.create",
            )
        with self.assertRaisesRegex(
            WorkspaceIntegrityError, "sole current Mission-wide Strategy identity"
        ):
            self._commit_strategy(
                strategy_id="strategy.ambiguous",
                meaning="second current head must be rejected atomically",
                command_id="strategy.ambiguous.create",
            )
        self.assertEqual(
            self.store.read_mission_strategy_head("mission.1")["strategy_id"],
            "strategy.theta.1",
        )

        with self.assertRaisesRegex(ValueError, "mission_id"):
            self.store.commit_branch_revision(
                executive_epoch_id=self.executive_epoch_id,
                mission_id="mission.1",
                branch_id="branch.mismatch",
                payload={
                    "branch_id": "branch.mismatch",
                    "mission_id": "mission.2",
                },
                expected_head_revision=None,
                expected_head_payload_digest=None,
                lease=self.lease,
                command_id="branch.mismatch",
                actor="frontier-owner-test",
                expected_canonical_authority_digest=self.authority_digest,
            )
        with self.assertRaisesRegex(WorkspaceIntegrityError, "exact closed"):
            self._commit_candidate(
                candidate_id="candidate.a1",
                meaning="must not split Candidate and A1",
                command_id="candidate.a1.rejected",
                a1_requirement={"route": "A1"},
            )

    def _full_rh_candidate_payload(
        self,
        candidate_id: str = "candidate.complete-rh",
    ) -> dict[str, object]:
        return {
            "schema_version": 4,
            "kind": "candidate_revision",
            "contract_schema_sha256": candidate_schema_digest_v4(REPO_ROOT),
            "candidate_id": candidate_id,
            "mission_id": "mission.1",
            "proposal_kind": "mathematical_statement",
            "exact_statement": (
                "This Candidate proves the Riemann Hypothesis and supplies a "
                "complete unconditional proof."
            ),
            "scope_and_reach": "complete unconditional proof of RH",
            "complete_target_claim": {
                "target": "riemann_hypothesis",
                "disposition": "proof",
            },
            "standing": {
                "status": "open",
                "basis": "unverified Candidate requiring independent Admission",
            },
            "provenance": {
                "kind": "native_executive_epoch",
                "executive_epoch_id": self.executive_epoch_id,
                "executive_epoch_authority_sha256": (
                    self.executive_epoch_authority_sha256
                ),
            },
            "authority_class": "candidate_only",
            "canonical_effect": "none",
        }

    def _ordinary_candidate_v2_payload(
        self,
        *,
        candidate_id: str,
        executive_epoch_id: str,
        executive_epoch_authority_sha256: str,
    ) -> dict[str, object]:
        return {
            "schema_version": 2,
            "kind": "candidate_revision",
            "contract_schema_sha256": candidate_schema_digest(REPO_ROOT),
            "candidate_id": candidate_id,
            "mission_id": "mission.1",
            "proposal_kind": "lemma",
            "exact_statement": (
                "A localized transform estimate may isolate one obstruction."
            ),
            "scope_and_reach": "one local diagnostic estimate",
            "standing": {
                "status": "open",
                "basis": "native executive research Candidate",
            },
            "provenance": {
                "kind": "native_executive_epoch",
                "executive_epoch_id": executive_epoch_id,
                "executive_epoch_authority_sha256": (
                    executive_epoch_authority_sha256
                ),
            },
            "authority_class": "candidate_only",
            "canonical_effect": "none",
        }

    def _seed_historical_candidate_v2(
        self,
        payload: dict[str, object],
        *,
        command_id: str,
    ):
        """Materialize retained v2 history without using the current write API."""

        candidate_id = str(payload["candidate_id"])
        write = RevisionWrite(
            TypedWorkspaceId(IdentityKind.CANDIDATE, candidate_id),
            payload,
            expected_revision=None,
        )
        with patch.object(
            self.store,
            "_validate_current_successor_candidate_a1",
            return_value=None,
        ):
            return self.store._apply_successor_storage_command(
                family=RevisionCommandFamily.CANDIDATE,
                lease=self.lease,
                command_id=command_id,
                actor="frontier-owner-historical-fixture",
                command_kind="commit_candidate_revision",
                executive_epoch_id=self.executive_epoch_id,
                request={
                    "mission_id": "mission.1",
                    "candidate_id": candidate_id,
                    "historical_fixture": True,
                },
                semantic_payload=payload,
                writes=(write,),
                auxiliary_writes=(),
                evidence_head_advances=(),
                target_head_key=write.object_id.key,
                expected_target_revision=None,
                expected_target_payload_digest=None,
                dependency_heads=None,
                expected_canonical_authority_digest=self.authority_digest,
            )

    def _commit_full_rh_candidate(
        self,
        *,
        command_id: str,
        candidate_id: str = "candidate.complete-rh",
        expected_revision: int | None = None,
        expected_digest: str | None = None,
    ):
        payload = self._full_rh_candidate_payload(candidate_id)
        requirement = candidate_a1_requirement(payload)
        self.assertIsNotNone(requirement)
        return self.store.commit_candidate_revision(
            executive_epoch_id=self.executive_epoch_id,
            mission_id="mission.1",
            candidate_id=candidate_id,
            payload=payload,
            expected_head_revision=expected_revision,
            expected_head_payload_digest=expected_digest,
            a1_requirement=requirement,
            lease=self.lease,
            command_id=command_id,
            actor="frontier-owner-test",
            expected_canonical_authority_digest=self.authority_digest,
        )

    def test_complete_candidate_a1_is_atomic_replayable_and_noop_safe(self) -> None:
        before = self.store.read_metadata()
        created = self._commit_full_rh_candidate(command_id="candidate.a1.atomic")
        self.assertFalse(created.replayed)
        self.assertEqual(
            created.project_commit,
            int(before["current_project_commit"]) + 1,
        )
        stored = self.store.read_candidate_revision(
            mission_id="mission.1",
            candidate_id="candidate.complete-rh",
        )
        binding = self.store.read_candidate_a1_binding(
            candidate_id="candidate.complete-rh",
            candidate_revision=int(stored["revision"]),
            candidate_digest=str(stored["payload_digest"]),
        )
        binding.verify_issued()
        self.assertEqual(binding.artifact_digest, stored["payload_digest"])
        self.assertEqual(binding.hold_lifecycle, "open")
        self.assertEqual(binding.canonical_effect, "none")
        self.assertEqual(binding.mathematical_effect, "none")

        def open_candidate_keys():
            return tuple(
                (
                    item.candidate_id,
                    item.candidate_revision,
                    item.candidate_digest,
                )
                for item in self.store._list_open_mission_candidate_a1_bindings(
                    "mission.1"
                )
            )

        expected_open = (
            (
                "candidate.complete-rh",
                int(stored["revision"]),
                str(stored["payload_digest"]),
            ),
        )
        self.assertEqual(open_candidate_keys(), expected_open)

        with self.store.snapshot_connection() as connection:
            self.assertEqual(
                int(
                    connection.execute(
                        "SELECT COUNT(*) FROM evidence_item_head WHERE evidence_id = ?",
                        (binding.evidence_id,),
                    ).fetchone()[0]
                ),
                1,
            )
            self.assertEqual(
                int(
                    connection.execute(
                        "SELECT COUNT(*) FROM alert_event WHERE alert_id = ?",
                        (binding.alert_id,),
                    ).fetchone()[0]
                ),
                1,
            )
            self.assertEqual(
                int(
                    connection.execute(
                        "SELECT COUNT(*) FROM hold WHERE hold_id = ? AND lifecycle = 'open'",
                        (binding.hold_id,),
                    ).fetchone()[0]
                ),
                1,
            )

        replay = self._commit_full_rh_candidate(command_id="candidate.a1.atomic")
        self.assertTrue(replay.replayed)
        self.assertEqual(replay.project_commit, created.project_commit)
        self.assertEqual(open_candidate_keys(), expected_open)

        metadata_before_noop = self.store.read_metadata()
        with self.store.snapshot_connection() as connection:
            journal_before_noop = int(
                connection.execute(
                    "SELECT COUNT(*) FROM transition_journal"
                ).fetchone()[0]
            )
        no_op = self._commit_full_rh_candidate(
            command_id="candidate.a1.semantic-noop",
            expected_revision=int(stored["revision"]),
            expected_digest=str(stored["payload_digest"]),
        )
        self.assertTrue(no_op.replayed)
        self.assertTrue(no_op.result["semantic_noop"])
        self.assertEqual(
            self.store.read_metadata()["current_project_commit"],
            metadata_before_noop["current_project_commit"],
        )
        with self.store.snapshot_connection() as connection:
            self.assertEqual(
                int(
                    connection.execute(
                        "SELECT COUNT(*) FROM transition_journal"
                    ).fetchone()[0]
                ),
                journal_before_noop,
            )
            self.assertIsNone(
                connection.execute(
                    "SELECT 1 FROM command_result WHERE command_id = ?",
                    ("candidate.a1.semantic-noop",),
                ).fetchone()
            )
        self.assertEqual(open_candidate_keys(), expected_open)

    def test_v4_complete_claim_opens_a1_and_later_removal_preserves_history(
        self,
    ) -> None:
        candidate_id = "candidate.v4-complete-rh"
        v2 = self._ordinary_candidate_v2_payload(
            candidate_id=candidate_id,
            executive_epoch_id=self.executive_epoch_id,
            executive_epoch_authority_sha256=(
                self.executive_epoch_authority_sha256
            ),
        )
        metadata_before_rejection = self.store.read_metadata()
        with self.assertRaisesRegex(
            WorkspaceIntegrityError,
            "Candidate-v4.*historical read/recovery",
        ):
            self.store.commit_candidate_revision(
                executive_epoch_id=self.executive_epoch_id,
                mission_id="mission.1",
                candidate_id=candidate_id,
                payload=v2,
                expected_head_revision=None,
                expected_head_payload_digest=None,
                a1_requirement=None,
                lease=self.lease,
                command_id="candidate.v2.current-rejected",
                actor="frontier-owner-test",
                expected_canonical_authority_digest=self.authority_digest,
            )
        self.assertEqual(self.store.read_metadata(), metadata_before_rejection)
        self.assertIsNone(
            self.store.get_head(
                TypedWorkspaceId(IdentityKind.CANDIDATE, candidate_id)
            )
        )
        retained_v3 = {
            **v2,
            "schema_version": 3,
            "contract_schema_sha256": candidate_schema_digest_v3(REPO_ROOT),
        }
        with self.assertRaisesRegex(
            WorkspaceIntegrityError, "Candidate-v4.*historical read/recovery"
        ):
            self.store.commit_candidate_revision(
                executive_epoch_id=self.executive_epoch_id,
                mission_id="mission.1",
                candidate_id=candidate_id,
                payload=retained_v3,
                expected_head_revision=None,
                expected_head_payload_digest=None,
                a1_requirement=None,
                lease=self.lease,
                command_id="candidate.v3.current-rejected",
                actor="frontier-owner-test",
                expected_canonical_authority_digest=self.authority_digest,
            )
        self.assertEqual(self.store.read_metadata(), metadata_before_rejection)
        first = self._seed_historical_candidate_v2(
            v2,
            command_id="candidate.v4.evolution.historical-v2-fixture",
        )
        first_readback = self.store.read_candidate_revision(
            mission_id="mission.1",
            candidate_id=candidate_id,
        )
        self.assertEqual(first_readback["revision"], 1)
        self.assertEqual(first_readback["payload"]["schema_version"], 2)

        v4 = dict(v2)
        v4["schema_version"] = 4
        v4["contract_schema_sha256"] = candidate_schema_digest_v4(REPO_ROOT)
        v4["complete_target_claim"] = {
            "target": "riemann_hypothesis",
            "disposition": "proof",
        }
        requirement = candidate_a1_requirement(v4)
        self.assertIsNotNone(requirement)
        opened = self.store.commit_candidate_revision(
            executive_epoch_id=self.executive_epoch_id,
            mission_id="mission.1",
            candidate_id=candidate_id,
            payload=v4,
            expected_head_revision=1,
            expected_head_payload_digest=str(first_readback["payload_digest"]),
            a1_requirement=requirement,
            lease=self.lease,
            command_id="candidate.v4.evolution.open-a1",
            actor="frontier-owner-test",
            expected_canonical_authority_digest=self.authority_digest,
        )
        self.assertEqual(opened.project_commit, first.project_commit + 1)
        complete_readback = self.store.read_candidate_revision(
            mission_id="mission.1",
            candidate_id=candidate_id,
        )
        historical_binding = self.store.read_candidate_a1_binding(
            candidate_id=candidate_id,
            candidate_revision=2,
            candidate_digest=str(complete_readback["payload_digest"]),
        )
        historical_binding.verify_issued()

        withdrawn = dict(v4)
        del withdrawn["complete_target_claim"]
        withdrawn["standing"] = {
            "status": "withdrawn",
            "basis": "the purported complete reach was withdrawn",
        }
        self.assertIsNone(candidate_a1_requirement(withdrawn))
        self.store.commit_candidate_revision(
            executive_epoch_id=self.executive_epoch_id,
            mission_id="mission.1",
            candidate_id=candidate_id,
            payload=withdrawn,
            expected_head_revision=2,
            expected_head_payload_digest=str(complete_readback["payload_digest"]),
            a1_requirement=None,
            lease=self.lease,
            command_id="candidate.v4.evolution.withdraw",
            actor="frontier-owner-test",
            expected_canonical_authority_digest=self.authority_digest,
        )
        current = self.store.read_candidate_revision(
            mission_id="mission.1",
            candidate_id=candidate_id,
        )
        self.assertEqual(current["revision"], 3)
        self.assertNotIn("complete_target_claim", current["payload"])
        retained = [
            binding
            for binding in self.store._list_open_mission_candidate_a1_bindings(
                "mission.1"
            )
            if binding.candidate_id == candidate_id
        ]
        self.assertEqual(
            [(binding.candidate_id, binding.candidate_revision) for binding in retained],
            [(candidate_id, 2)],
        )
        self.assertEqual(retained[0].evidence_id, historical_binding.evidence_id)

        reopened = WorkspaceStore.open(
            self.store.paths,
            expected_project_id="project.rh",
        )
        reconstructed = [
            binding
            for binding in reopened._list_open_mission_candidate_a1_bindings(
                "mission.1"
            )
            if binding.candidate_id == candidate_id
        ]
        self.assertEqual(len(reconstructed), 1)
        reconstructed[0].verify_issued()
        self.assertEqual(reconstructed[0].candidate_revision, 2)
        self.assertEqual(reconstructed[0].hold_lifecycle, "open")
        self.assertEqual(reconstructed[0].evidence_id, historical_binding.evidence_id)

    def test_complete_candidate_a1_rolls_back_every_database_effect(self) -> None:
        tracked_tables = (
            "candidate_head",
            "evidence_item_head",
            "alert_event",
            "hold",
            "transition_journal",
            "command_result",
        )
        with self.store.snapshot_connection() as connection:
            baseline_counts = {
                table: int(
                    connection.execute(
                        f"SELECT COUNT(*) FROM {table}"
                    ).fetchone()[0]
                )
                for table in tracked_tables
            }
            baseline_projection = tuple(
                tuple(row)
                for row in connection.execute(
                    "SELECT * FROM current_dependency_projection"
                )
            )
            baseline_nodes = tuple(
                tuple(row)
                for row in connection.execute(
                    "SELECT * FROM current_dependency_node ORDER BY scope_key"
                )
            )
        payload = self._full_rh_candidate_payload()
        with self.assertRaisesRegex(WorkspaceIntegrityError, "requires atomic"):
            self.store.commit_candidate_revision(
                executive_epoch_id=self.executive_epoch_id,
                mission_id="mission.1",
                candidate_id="candidate.complete-rh",
                payload=payload,
                expected_head_revision=None,
                expected_head_payload_digest=None,
                a1_requirement=None,
                lease=self.lease,
                command_id="candidate.a1.omitted",
                actor="frontier-owner-test",
                expected_canonical_authority_digest=self.authority_digest,
            )
        malformed_requirement = dict(candidate_a1_requirement(payload) or {})
        malformed_requirement["extra"] = "forbidden"
        with self.assertRaisesRegex(WorkspaceIntegrityError, "exact closed"):
            self.store.commit_candidate_revision(
                executive_epoch_id=self.executive_epoch_id,
                mission_id="mission.1",
                candidate_id="candidate.complete-rh",
                payload=payload,
                expected_head_revision=None,
                expected_head_payload_digest=None,
                a1_requirement=malformed_requirement,
                lease=self.lease,
                command_id="candidate.a1.malformed",
                actor="frontier-owner-test",
                expected_canonical_authority_digest=self.authority_digest,
            )

        with patch.object(
            WorkspaceStore,
            "_validate_current_successor_candidate_a1",
            side_effect=WorkspaceIntegrityError("injected atomic A1 failure"),
        ):
            with self.assertRaisesRegex(WorkspaceIntegrityError, "injected"):
                self._commit_full_rh_candidate(command_id="candidate.a1.rollback")

        with self.store.snapshot_connection() as connection:
            for table in tracked_tables:
                self.assertEqual(
                    int(connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]),
                    baseline_counts[table],
                    table,
                )
            self.assertEqual(
                tuple(
                    tuple(row)
                    for row in connection.execute(
                        "SELECT * FROM current_dependency_projection"
                    )
                ),
                baseline_projection,
            )
            self.assertEqual(
                tuple(
                    tuple(row)
                    for row in connection.execute(
                        "SELECT * FROM current_dependency_node ORDER BY scope_key"
                    )
                ),
                baseline_nodes,
            )
        recovered = self._commit_full_rh_candidate(
            command_id="candidate.a1.rollback"
        )
        self.assertFalse(recovered.replayed)

    def test_branch_and_strategy_advance_independently(self) -> None:
        self._commit_branch(
            branch_id="branch.alpha",
            meaning="branch revision one",
            command_id="branch.alpha.create",
        )
        opening_strategy = self.store.read_mission_strategy_head("mission.1")
        self._commit_strategy(
            strategy_id="strategy.theta.1",
            meaning="strategy revision two",
            command_id="strategy.theta.independent-update-one",
            expected_revision=int(opening_strategy["revision"]),
            expected_digest=str(opening_strategy["payload_digest"]),
        )
        branch_one = self.store.read_branch_revision(
            mission_id="mission.1",
            branch_id="branch.alpha",
        )
        strategy_one = self.store.read_strategy_revision(
            mission_id="mission.1",
            strategy_id="strategy.theta.1",
        )
        self._commit_branch(
            branch_id="branch.alpha",
            meaning="branch revision two",
            command_id="branch.alpha.update",
            expected_revision=int(branch_one["revision"]),
            expected_digest=str(branch_one["payload_digest"]),
        )
        self.assertEqual(
            self.store.read_strategy_revision(
                mission_id="mission.1",
                strategy_id="strategy.theta.1",
            )["payload_digest"],
            strategy_one["payload_digest"],
        )
        branch_two = self.store.read_branch_revision(
            mission_id="mission.1",
            branch_id="branch.alpha",
        )
        self._commit_strategy(
            strategy_id="strategy.theta.1",
            meaning="strategy revision three",
            command_id="strategy.theta.independent-update-two",
            expected_revision=int(strategy_one["revision"]),
            expected_digest=str(strategy_one["payload_digest"]),
        )
        self.assertEqual(
            self.store.read_branch_revision(
                mission_id="mission.1",
                branch_id="branch.alpha",
                revision=1,
            )["payload"]["meaning"],
            "branch revision one",
        )
        self.assertEqual(
            self.store.read_strategy_revision(
                mission_id="mission.1",
                strategy_id="strategy.theta.1",
                revision=2,
            )["payload"]["meaning"],
            "strategy revision two",
        )
        self.assertEqual(
            self.store.read_branch_revision(
                mission_id="mission.1",
                branch_id="branch.alpha",
            )["payload_digest"],
            branch_two["payload_digest"],
        )

    def _derived_record(self, evidence_id: str, revision: int) -> EvidenceItemRevision:
        return EvidenceItemRevision(
            evidence_id=evidence_id,
            revision=revision,
            subtype="synthesis_derivation",
            subject={"mission_id": "mission.1", "claim": "one derived relation"},
            exact_scope="one exact derived relation over cited owner revisions",
            rigor="derived",
            limitations=("limited to the cited exact revisions",),
            non_inferences=("does not establish the Riemann Hypothesis",),
            security_classification="internal",
            retention="mission",
            blob_roles=(),
            availability_state="verified_available",
        )

    def test_reader_preserves_retained_legacy_evidence_blob_roles(self) -> None:
        blob_bytes = b"retained legacy Evidence material"
        blob_sha256 = hashlib.sha256(blob_bytes).hexdigest()
        evidence = EvidenceItemRevision(
            evidence_id="evidence.retained-legacy",
            revision=1,
            subtype="native_context_judgment",
            subject={
                "mission_id": "mission.1",
                "statement": "One retained predecessor judgment.",
            },
            exact_scope="the exact retained predecessor judgment",
            rigor="model_judgment",
            limitations=("retained historical interpretation",),
            non_inferences=("does not establish the Riemann Hypothesis",),
            security_classification="internal",
            retention="mission",
            blob_roles=(("native_material", blob_sha256),),
            availability_state="verified_available",
        )
        payload = canonical_payload(evidence.to_payload())
        from dataclasses import replace
        from research_core import workspace_store as store_module

        cas = EvidenceCAS(self.store.paths)
        blob = replace(
            cas.ingest_bytes(
                blob_bytes, original_name="retained-legacy.txt",
                media_type="text/plain", encoding="utf-8",
            ).record,
            availability_state="verified_available",
        )
        capture_id = store_module.canonical_raw_capture_id(
            project_id=self.store.project_id, capture_kind="output",
            observation_id="retained-legacy.material",
        )
        self.store.commit_raw_capture(
            record={
                "capture_id": capture_id, "mission_id": "mission.1",
                "executive_epoch_id": self.executive_epoch_id,
                "capture_kind": "output", "observation_id": "retained-legacy.material",
                "assignment_id": "retained-legacy.fixture",
                "provenance": {"kind": "retained_history_fixture"},
                "completion": {"lifecycle": "completed"},
            },
            artifacts=({
                "ordinal": 0, "role": "native_material",
                "logical_name": "retained-legacy.txt", "blob_sha256": blob_sha256,
            },),
            blobs=(blob,), lease=self.lease, command_id="retained-legacy.capture",
            actor="frontier-owner-historical-fixture",
        )
        source = {
            "source_ordinal": 0, "capture_id": capture_id, "artifact_ordinal": 0,
            "exact_scope": {"scope": evidence.exact_scope},
        }
        table = store_module.AuxiliaryTable
        auxiliary = (
            store_module.AuxiliaryWrite(table.EVIDENCE_ITEM_REVISION, {
                "evidence_id": evidence.evidence_id, "revision": evidence.revision,
                "subtype": evidence.subtype, "subject_json": evidence.subject,
                "exact_scope_json": {"scope": evidence.exact_scope},
                "rigor": evidence.rigor, "limitations_json": list(evidence.limitations),
                "non_inferences_json": list(evidence.non_inferences),
                "security_json": {"classification": evidence.security_classification},
                "retention_json": {"policy": evidence.retention},
                "availability_state": evidence.availability_state,
                "payload_digest": payload.sha256,
            }),
            store_module.AuxiliaryWrite(table.EVIDENCE_BLOB, {
                "evidence_id": evidence.evidence_id, "evidence_revision": evidence.revision,
                "ordinal": 0, "role": "native_material", "blob_sha256": blob_sha256,
            }),
            store_module.AuxiliaryWrite(table.EVIDENCE_CAPTURE_SOURCE, {
                "evidence_id": evidence.evidence_id, "evidence_revision": evidence.revision,
                "source_ordinal": 0, "capture_id": capture_id, "artifact_ordinal": 0,
                "exact_scope_json": source["exact_scope"],
            }),
        )
        semantic_record = dict(evidence.to_payload())
        semantic_record.pop("revision")
        semantic_payload = {
            "record": semantic_record, "sources": [source], "interpreted_inputs": [],
        }
        current_scope = store_module._validate_family_scope

        def retained_scope(**scope):
            # Like the retained Candidate-v2 fixture above, seed historical
            # bytes through the real transaction. Only the current prohibition
            # of legacy private Blob roles is bypassed; exact row, journal,
            # head, physical CAS and read verification remain enabled.
            self.assertEqual(scope["family"], RevisionCommandFamily.EVIDENCE)
            self.assertEqual(scope["command_kind"], "commit_evidence_meaning_revision")
            links = tuple(item for item in scope["auxiliary_writes"] if item.table is table.EVIDENCE_BLOB)
            self.assertEqual(links, (auxiliary[1],))
            current_scope(**{
                **scope,
                "auxiliary_writes": tuple(
                    item for item in scope["auxiliary_writes"] if item.table is not table.EVIDENCE_BLOB
                ),
            })

        with patch.object(store_module, "_validate_family_scope", side_effect=retained_scope):
            self.store._apply_successor_storage_command(
                family=RevisionCommandFamily.EVIDENCE, lease=self.lease,
                command_id="retained-legacy.evidence", actor="frontier-owner-historical-fixture",
                command_kind="commit_evidence_meaning_revision",
                executive_epoch_id=self.executive_epoch_id,
                request={"evidence_id": evidence.evidence_id, "historical_fixture": True},
                semantic_payload=semantic_payload, auxiliary_writes=auxiliary,
                evidence_head_advances=(store_module.EvidenceHeadAdvance(
                    evidence_id=evidence.evidence_id, target_revision=evidence.revision,
                    target_payload_digest=payload.sha256, expected_revision=None,
                    expected_payload_digest=None,
                ),),
                target_head_key=f"evidence:{evidence.evidence_id}",
                expected_canonical_authority_digest=self.authority_digest,
            )
        self.store.verify_integrity()

        stored = self.store.read_evidence_meaning_revision(evidence.evidence_id)

        self.assertEqual(
            canonical_json_bytes(stored["record"]),
            canonical_json_bytes(evidence.to_payload()),
        )
        self.assertEqual(stored["payload_digest"], payload.sha256)
        self.assertEqual(stored["record"]["blob_roles"], evidence.blob_roles)
        recovered = MissionInterface(
            store=self.store,
            cas=EvidenceCAS(self.store.paths),
            expected_mission_id="mission.1",
        )._read_recovery_owner_revision(
            kind="evidence",
            identity=evidence.evidence_id,
            revision=evidence.revision,
        )
        self.assertEqual(recovered["reference"]["payload_sha256"], payload.sha256)
        self.assertEqual(
            canonical_json_bytes(recovered["document"]),
            canonical_json_bytes(evidence.to_payload()),
        )

    def test_derived_evidence_binds_exact_current_and_historical_inputs(self) -> None:
        for candidate_id in ("candidate.alpha", "candidate.beta"):
            self._commit_candidate(
                candidate_id=candidate_id,
                meaning=f"interpreted premise {candidate_id}",
                command_id=f"{candidate_id}.create",
            )
        alpha = self.store.read_candidate_revision(
            mission_id="mission.1",
            candidate_id="candidate.alpha",
        )
        beta = self.store.read_candidate_revision(
            mission_id="mission.1",
            candidate_id="candidate.beta",
        )
        inputs = (
            {
                "kind": "candidate",
                "identity": "candidate.alpha",
                "revision": int(alpha["revision"]),
                "payload_sha256": str(alpha["payload_digest"]),
            },
            {
                "kind": "candidate",
                "identity": "candidate.beta",
                "revision": int(beta["revision"]),
                "payload_sha256": str(beta["payload_digest"]),
            },
        )
        dependencies = {
            f"candidate:{item['identity']}": (
                int(item["revision"]),
                str(item["payload_sha256"]),
            )
            for item in inputs
        }
        with self.assertRaisesRegex(StaleCommandError, "not dependency-bound"):
            self.store.commit_evidence_meaning_revision(
                executive_epoch_id=self.executive_epoch_id,
                record=self._derived_record("evidence.unbound", 1),
                sources=(),
                interpreted_inputs=inputs,
                expected_head_revision=None,
                expected_head_payload_digest=None,
                expected_dependency_heads=None,
                lease=self.lease,
                command_id="evidence.unbound.create",
                actor="frontier-owner-test",
                expected_canonical_authority_digest=self.authority_digest,
            )

        record = self._derived_record("evidence.synthesis", 1)
        committed = self.store.commit_evidence_meaning_revision(
            executive_epoch_id=self.executive_epoch_id,
            record=record,
            sources=(),
            interpreted_inputs=inputs,
            expected_head_revision=None,
            expected_head_payload_digest=None,
            expected_dependency_heads=dependencies,
            lease=self.lease,
            command_id="evidence.synthesis.create",
            actor="frontier-owner-test",
            expected_canonical_authority_digest=self.authority_digest,
        )
        self.assertFalse(committed.replayed)
        replay = self.store.commit_evidence_meaning_revision(
            executive_epoch_id=self.executive_epoch_id,
            record=record,
            sources=(),
            interpreted_inputs=inputs,
            expected_head_revision=None,
            expected_head_payload_digest=None,
            expected_dependency_heads=dependencies,
            lease=self.lease,
            command_id="evidence.synthesis.create",
            actor="frontier-owner-test",
            expected_canonical_authority_digest=self.authority_digest,
        )
        self.assertTrue(replay.replayed)
        stored = self.store.read_evidence_meaning_revision("evidence.synthesis")
        self.assertEqual(stored["sources"], ())
        self.assertEqual(tuple(stored["interpreted_inputs"]), inputs)
        self.assertEqual(
            tuple(stored["record"]["subject"]["interpreted_owner_inputs"]),
            inputs,
        )
        metadata_before_noop = self.store.read_metadata()
        with self.store.snapshot_connection() as connection:
            journal_before_noop = int(
                connection.execute(
                    "SELECT COUNT(*) FROM transition_journal"
                ).fetchone()[0]
            )
        semantic_noop = self.store.commit_evidence_meaning_revision(
            executive_epoch_id=self.executive_epoch_id,
            record=self._derived_record("evidence.synthesis", 2),
            sources=(),
            interpreted_inputs=inputs,
            expected_head_revision=1,
            expected_head_payload_digest=str(stored["payload_digest"]),
            expected_dependency_heads=dependencies,
            lease=self.lease,
            command_id="evidence.synthesis.noop",
            actor="frontier-owner-test",
            expected_canonical_authority_digest=self.authority_digest,
        )
        self.assertTrue(semantic_noop.replayed)
        self.assertTrue(semantic_noop.result["semantic_noop"])
        self.assertEqual(
            self.store.read_metadata()["current_project_commit"],
            metadata_before_noop["current_project_commit"],
        )
        with self.store.snapshot_connection() as connection:
            self.assertEqual(
                int(
                    connection.execute(
                        "SELECT COUNT(*) FROM transition_journal"
                    ).fetchone()[0]
                ),
                journal_before_noop,
            )
            self.assertIsNone(
                connection.execute(
                    "SELECT 1 FROM command_result WHERE command_id = ?",
                    ("evidence.synthesis.noop",),
                ).fetchone()
            )

        self._commit_candidate(
            candidate_id="candidate.alpha",
            meaning="alpha revision two",
            command_id="candidate.alpha.update",
            expected_revision=int(alpha["revision"]),
            expected_digest=str(alpha["payload_digest"]),
        )
        historical = self.store.commit_evidence_meaning_revision(
            executive_epoch_id=self.executive_epoch_id,
            record=self._derived_record("evidence.historical", 1),
            sources=(),
            interpreted_inputs=inputs,
            expected_head_revision=None,
            expected_head_payload_digest=None,
            expected_dependency_heads={
                "candidate:candidate.beta": (
                    int(beta["revision"]),
                    str(beta["payload_digest"]),
                )
            },
            lease=self.lease,
            command_id="evidence.historical.create",
            actor="frontier-owner-test",
            expected_canonical_authority_digest=self.authority_digest,
        )
        self.assertFalse(historical.replayed)
        with self.assertRaisesRegex(ValueError, "at least two"):
            self.store.commit_evidence_meaning_revision(
                executive_epoch_id=self.executive_epoch_id,
                record=self._derived_record("evidence.too-few", 1),
                sources=(),
                interpreted_inputs=inputs[:1],
                expected_head_revision=None,
                expected_head_payload_digest=None,
                lease=self.lease,
                command_id="evidence.too-few.create",
                actor="frontier-owner-test",
                expected_canonical_authority_digest=self.authority_digest,
            )



if __name__ == "__main__":
    unittest.main()
