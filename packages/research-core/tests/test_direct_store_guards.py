from __future__ import annotations

import hashlib
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any, Mapping


PACKAGE_ROOT = Path(__file__).resolve().parents[1]
TEST_ROOT = Path(__file__).resolve().parent
REPO_ROOT = Path(__file__).resolve().parents[3]
for candidate in (PACKAGE_ROOT, TEST_ROOT):
    if str(candidate) not in sys.path:
        sys.path.insert(0, str(candidate))

from research_core.candidate_revision import prepare_candidate_revision  # noqa: E402
from research_core.candidate_a1_triage import CandidateA1Ref  # noqa: E402
from research_core.canonical_snapshot import load_canonical_snapshot  # noqa: E402
from research_core.json_support import canonical_json_bytes  # noqa: E402
from research_core.mission_executive import (  # noqa: E402
    DirectContinuationCheckpoint,
    OwnerRevisionRef,
    owner_revision_ref_from_mapping,
    prepare_direct_continuation_checkpoint,
    prepare_direct_executive_epoch_checkpointed_event,
    reissue_direct_executive_epoch_authority,
)
from research_core.mission_frontier import (  # noqa: E402
    commit_strategy_revision,
    prepare_strategy_revision,
    read_mission_strategy_head,
)
from research_core.mission_interface import MissionInterface  # noqa: E402
from research_core.research_model import deep_thaw  # noqa: E402
from research_core.workspace_paths import attest_current_principal  # noqa: E402
from research_core.workspace_store import (  # noqa: E402
    StaleCommandError,
    WorkspaceIntegrityError,
)
from test_mission_interface_direct import _direct_genesis_fixture  # noqa: E402


def _digest(document: Mapping[str, Any]) -> str:
    return hashlib.sha256(canonical_json_bytes(document)).hexdigest()


class DirectStoreGuardTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        loaded = load_canonical_snapshot(REPO_ROOT, source_commit="a" * 40)
        if not loaded.ok or loaded.value is None:
            raise AssertionError(loaded.failure)
        cls.canonical = loaded.value

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.interface = MissionInterface.initialize_from_owner(
            Path(self.temporary.name) / "mission",
            project_id="project.rh",
            mission_id="mission.1",
            canonical_snapshot=self.canonical,
            genesis_seed=_direct_genesis_fixture(),
            owner_command_id="direct-store-guards.genesis",
        )
        self.store = self.interface._store

    def _bind(self, *, goal_name: str = "goal"):
        authorized = self.interface.authorize_executive_epoch_from_owner(
            {"expected_cut": self.interface.host_snapshot()["authorization_cut"]}
        )
        epoch_id = str(authorized["executive_epoch_id"])
        goal_workspace = Path(self.temporary.name) / goal_name
        goal_workspace.mkdir()
        self.interface.bind_executive_epoch_from_owner(
            {
                "executiveEpochId": epoch_id,
                "rootThreadId": (
                    "goal-thread:store-guards"
                    if goal_name == "goal"
                    else f"goal-thread:store-guards:{goal_name}"
                ),
                "workspaceRoot": str(goal_workspace.resolve()),
            }
        )
        chain = self.store.read_active_executive_epoch("mission.1")
        self.assertIsNotNone(chain)
        metadata = self.store.read_metadata()
        return reissue_direct_executive_epoch_authority(
            event_readbacks=chain,
            project_id=str(metadata["project_id"]),
            root_identity=str(metadata["root_identity"]),
            canonical_authority_digest=str(metadata["canonical_authority_digest"]),
        )

    def _checkpoint(self, authority):
        strategy = read_mission_strategy_head(
            self.store,
            mission_id="mission.1",
        )
        strategy_root = OwnerRevisionRef(
            "strategy",
            str(strategy.record.document["strategy_id"]),
            strategy.revision,
            strategy.record.payload_sha256,
        )
        metadata = self.store.read_metadata()
        return prepare_direct_continuation_checkpoint(
            mission_root=owner_revision_ref_from_mapping(authority.mission_root),
            strategy_root=strategy_root,
            resolver=self.interface._resolve_direct_checkpoint_owner,
            predecessor_checkpoint=authority.predecessor_checkpoint,
            authoring_epoch_id=authority.executive_epoch_id,
            project_commit=int(metadata["current_project_commit"]) + 1,
            schema_version=1,
        )

    def _commit_checkpoint(self, authority, checkpoint, *, lease, command_id: str):
        event = prepare_direct_executive_epoch_checkpointed_event(checkpoint)
        return self.store.commit_direct_continuation_checkpoint(
            mission_id="mission.1",
            executive_epoch_id=authority.executive_epoch_id,
            checkpoint_document=checkpoint,
            event=event,
            expected_event_ordinal=authority.bound_event_ordinal,
            expected_event_digest=authority.bound_event_digest,
            lease=lease,
            command_id=command_id,
            actor="direct-store-guard-test",
            expected_canonical_authority_digest=(
                authority.canonical_authority_digest
            ),
        )

    def _v2_checkpoint(self, authority, *, unresolved_pointers=()):
        strategy = read_mission_strategy_head(
            self.store,
            mission_id="mission.1",
        )
        strategy_root = OwnerRevisionRef(
            "strategy",
            str(strategy.record.document["strategy_id"]),
            strategy.revision,
            strategy.record.payload_sha256,
        )
        metadata = self.store.read_metadata()
        return prepare_direct_continuation_checkpoint(
            mission_root=owner_revision_ref_from_mapping(authority.mission_root),
            strategy_root=strategy_root,
            unresolved_pointers=unresolved_pointers,
            predecessor_checkpoint=authority.predecessor_checkpoint,
            authoring_epoch_id=authority.executive_epoch_id,
            project_commit=int(metadata["current_project_commit"]) + 1,
            schema_version=2,
        )

    def _advance_strategy(self, authority, *, command_id: str) -> None:
        current = read_mission_strategy_head(
            self.store,
            mission_id="mission.1",
        )
        prepared = prepare_strategy_revision(
            self.store,
            project_id=authority.project_id,
            mission_id=authority.mission_id,
            strategy_id=str(current.record.document["strategy_id"]),
            mission_continuation="continue",
            integrated_comparison=(
                "Advance the current Strategy while preserving exact prior history."
            ),
            reconsideration_conditions=(
                {
                    "condition": "Reconsider when exact new evidence is retained.",
                    "owner_refs": (),
                },
            ),
        )
        commit_strategy_revision(
            self.store,
            authority=authority,
            prepared=prepared,
            lease=self.store.reissue_writer_lease(attest_current_principal()),
            actor="direct-store-guard-test",
            command_id=command_id,
        )

    def test_store_rejects_incomplete_or_unreachable_declared_checkpoint_cut(self) -> None:
        authority = self._bind()
        lease = self.store.reissue_writer_lease(attest_current_principal())
        with self.store.direct_checkpoint_write_scope():
            checkpoint = self._checkpoint(authority)
            self.assertTrue(checkpoint.document["transitive_owner_refs"])

            incomplete_document = deep_thaw(checkpoint.document)
            incomplete_document["transitive_owner_refs"] = []
            incomplete = DirectContinuationCheckpoint(
                incomplete_document,
                _digest(incomplete_document),
            )
            with self.assertRaisesRegex(
                WorkspaceIntegrityError,
                "exact complete Store closure",
            ):
                self._commit_checkpoint(
                    authority,
                    incomplete,
                    lease=lease,
                    command_id="checkpoint.incomplete-bypass",
                )

            current_branch = checkpoint.document["transitive_owner_refs"][0]
            unreachable_document = deep_thaw(checkpoint.document)
            unreachable_document["transitive_owner_refs"].append(
                {
                    "kind": "branch",
                    "identity": "branch.unreachable",
                    "revision": 1,
                    "payload_sha256": str(current_branch["payload_sha256"]),
                }
            )
            unreachable_document["transitive_owner_refs"] = sorted(
                unreachable_document["transitive_owner_refs"],
                key=lambda item: (
                    item["kind"],
                    item["identity"],
                    item["revision"],
                    item["payload_sha256"],
                ),
            )
            unreachable = DirectContinuationCheckpoint(
                unreachable_document,
                _digest(unreachable_document),
            )
            with self.assertRaisesRegex(
                WorkspaceIntegrityError,
                "exact complete Store closure",
            ):
                self._commit_checkpoint(
                    authority,
                    unreachable,
                    lease=lease,
                    command_id="checkpoint.unreachable-bypass",
                )

    def test_store_rejects_fabricated_pointer_in_exact_persisted_owner(self) -> None:
        authority = self._bind()
        lease = self.store.reissue_writer_lease(attest_current_principal())
        with self.store.direct_checkpoint_write_scope():
            checkpoint = self._checkpoint(authority)
            fabricated_document = deep_thaw(checkpoint.document)
            fabricated_document["causal_pointers"] = [
                {
                    "owner_ref": deep_thaw(checkpoint.document["strategy_root"]),
                    "json_pointer": "/fabricated",
                }
            ]
            fabricated = DirectContinuationCheckpoint(
                fabricated_document,
                _digest(fabricated_document),
            )
            with self.assertRaisesRegex(
                WorkspaceIntegrityError,
                "does not resolve in its exact owner",
            ):
                self._commit_checkpoint(
                    authority,
                    fabricated,
                    lease=lease,
                    command_id="checkpoint.fabricated-pointer-bypass",
                )

    def test_v2_unresolved_checkpoint_survives_historical_strategy_advance(
        self,
    ) -> None:
        authority = self._bind(goal_name="checkpoint-goal")
        candidate = self._candidate_payload(authority, "candidate.unresolved-history")
        candidate["objections"] = ["One exact current objection remains unresolved."]
        self._commit_candidate(
            authority,
            candidate,
            command_id="candidate.unresolved-history",
        )
        candidate_head = self.store.list_mission_candidate_heads("mission.1")[0]
        candidate_root = OwnerRevisionRef(
            "candidate",
            "candidate.unresolved-history",
            int(candidate_head["revision"]),
            str(candidate_head["payload_digest"]),
        )
        strategy = read_mission_strategy_head(
            self.store,
            mission_id="mission.1",
        )
        strategy_root = OwnerRevisionRef(
            "strategy",
            str(strategy.record.document["strategy_id"]),
            strategy.revision,
            strategy.record.payload_sha256,
        )
        metadata = self.store.read_metadata()
        checkpoint = prepare_direct_continuation_checkpoint(
            mission_root=owner_revision_ref_from_mapping(authority.mission_root),
            strategy_root=strategy_root,
            unresolved_pointers=(
                {
                    "owner_ref": candidate_root.to_mapping(),
                    "json_pointer": "/objections/0",
                },
            ),
            predecessor_checkpoint=authority.predecessor_checkpoint,
            authoring_epoch_id=authority.executive_epoch_id,
            project_commit=int(metadata["current_project_commit"]) + 1,
            schema_version=2,
        )
        checkpoint_lease = self.store.reissue_writer_lease(
            attest_current_principal()
        )
        with self.store.direct_checkpoint_write_scope():
            self._commit_checkpoint(
                authority,
                checkpoint,
                lease=checkpoint_lease,
                command_id="checkpoint.v2-unresolved",
            )

        successor = self._bind(goal_name="successor-goal")
        self._advance_strategy(
            successor,
            command_id="strategy.after-v2-checkpoint",
        )
        advanced_candidate = deep_thaw(candidate)
        advanced_candidate["objections"] = sorted(
            [
                *advanced_candidate["objections"],
                "A later objection must not rewrite the checkpointed pointer.",
            ]
        )
        advanced_candidate["provenance"]["executive_epoch_id"] = (
            successor.executive_epoch_id
        )
        advanced_candidate["provenance"]["executive_epoch_authority_sha256"] = (
            successor.authority_sha256
        )
        self.store.commit_candidate_revision(
            executive_epoch_id=successor.executive_epoch_id,
            mission_id="mission.1",
            candidate_id="candidate.unresolved-history",
            payload=advanced_candidate,
            expected_head_revision=int(candidate_head["revision"]),
            expected_head_payload_digest=str(candidate_head["payload_digest"]),
            dependency_heads={},
            a1_requirement=None,
            lease=self.store.reissue_writer_lease(attest_current_principal()),
            command_id="candidate.after-v2-checkpoint",
            actor="direct-store-guard-test",
            expected_canonical_authority_digest=(
                successor.canonical_authority_digest
            ),
        )

        self.store.verify_integrity()
        stored = self.store.read_continuation_checkpoint(
            checkpoint_id=str(checkpoint.document["checkpoint_id"])
        )
        self.assertIsNotNone(stored)
        assert stored is not None
        with self.store.direct_recovery_read_scope():
            facts = self.store.read_direct_recovery_facts(
                cut_project_commit=int(checkpoint.document["project_commit"])
            )
            sections = self.store.materialize_continuation_checkpoint_sections(
                stored,
                recovery_facts=facts,
            )
        self.assertEqual(
            deep_thaw(sections["unresolved_pointers"]),
            [
                {
                    "owner_ref": candidate_root.to_mapping(),
                    "json_pointer": "/objections/0",
                }
            ],
        )

    def test_store_accepts_v2_current_unresolved_owner_outside_strategy_closure(self) -> None:
        authority = self._bind()
        candidate = self._candidate_payload(authority, "candidate.unresolved")
        candidate["objections"] = ["The exact remaining obstruction is explicit."]
        self._commit_candidate(
            authority,
            candidate,
            command_id="candidate.unresolved-outside-closure",
        )
        stored = self.store.list_mission_candidate_heads("mission.1")[0]
        checkpoint = self._v2_checkpoint(
            authority,
            unresolved_pointers=(
                {
                    "owner_ref": {
                        "kind": "candidate",
                        "identity": "candidate.unresolved",
                        "revision": int(stored["revision"]),
                        "payload_sha256": str(stored["payload_digest"]),
                    },
                    "json_pointer": "/objections/0",
                },
            ),
        )
        lease = self.store.reissue_writer_lease(attest_current_principal())
        with self.store.direct_checkpoint_write_scope():
            self._commit_checkpoint(
                authority,
                checkpoint,
                lease=lease,
                command_id="checkpoint.v2-current-outside-closure",
            )

        persisted = self.store.read_continuation_checkpoint(
            checkpoint_id=str(checkpoint.document["checkpoint_id"])
        )
        self.assertEqual(
            deep_thaw(persisted["document"]["unresolved_pointers"]),
            deep_thaw(checkpoint.document["unresolved_pointers"]),
        )

    def test_store_rejects_incomplete_v2_unresolved_projection_atomically(self) -> None:
        authority = self._bind()
        candidate = self._candidate_payload(authority, "candidate.complete-cut")
        candidate["objections"] = sorted(
            [
                "A first exact obstruction remains unresolved.",
                "A second exact obstruction remains unresolved.",
            ]
        )
        self._commit_candidate(
            authority,
            candidate,
            command_id="candidate.complete-cut",
        )
        stored = self.store.list_mission_candidate_heads("mission.1")[0]
        owner_ref = {
            "kind": "candidate",
            "identity": "candidate.complete-cut",
            "revision": int(stored["revision"]),
            "payload_sha256": str(stored["payload_digest"]),
        }
        before_metadata = deep_thaw(self.store.read_metadata())
        before_events = deep_thaw(
            self.store.read_active_executive_epoch("mission.1")
        )
        lease = self.store.reissue_writer_lease(attest_current_principal())

        for label, pointers in (
            ("empty", ()),
            (
                "subset",
                (
                    {
                        "owner_ref": owner_ref,
                        "json_pointer": "/objections/0",
                    },
                ),
            ),
        ):
            with self.subTest(label=label):
                checkpoint = self._v2_checkpoint(
                    authority,
                    unresolved_pointers=pointers,
                )
                with self.store.direct_checkpoint_write_scope():
                    with self.assertRaisesRegex(
                        WorkspaceIntegrityError,
                        "exact complete current owner projection",
                    ):
                        self._commit_checkpoint(
                            authority,
                            checkpoint,
                            lease=lease,
                            command_id=f"checkpoint.v2-incomplete.{label}",
                        )
                self.assertIsNone(
                    self.store.read_continuation_checkpoint(
                        checkpoint_id=str(checkpoint.document["checkpoint_id"])
                    )
                )
                self.assertEqual(before_metadata, deep_thaw(self.store.read_metadata()))
                self.assertEqual(
                    before_events,
                    deep_thaw(self.store.read_active_executive_epoch("mission.1")),
                )

    def test_store_rejects_v2_unresolved_pointer_without_exact_owner(self) -> None:
        authority = self._bind()
        checkpoint = self._v2_checkpoint(
            authority,
            unresolved_pointers=(
                {
                    "owner_ref": {
                        "kind": "branch",
                        "identity": "branch.absent",
                        "revision": 1,
                        "payload_sha256": "f" * 64,
                    },
                    "json_pointer": "/revival_conditions/0",
                },
            ),
        )
        lease = self.store.reissue_writer_lease(attest_current_principal())
        with self.store.direct_checkpoint_write_scope():
            with self.assertRaisesRegex(
                WorkspaceIntegrityError,
                "not an exact Store-backed owner",
            ):
                self._commit_checkpoint(
                    authority,
                    checkpoint,
                    lease=lease,
                    command_id="checkpoint.v2-absent-owner",
                )

    def test_store_rejects_v2_unresolved_pointer_missing_from_exact_owner(self) -> None:
        authority = self._bind()
        candidate = self._candidate_payload(authority, "candidate.no-objections")
        self._commit_candidate(
            authority,
            candidate,
            command_id="candidate.no-objections",
        )
        candidate_head = self.store.list_mission_candidate_heads("mission.1")[0]
        checkpoint = self._v2_checkpoint(
            authority,
            unresolved_pointers=(
                {
                    "owner_ref": {
                        "kind": "candidate",
                        "identity": "candidate.no-objections",
                        "revision": int(candidate_head["revision"]),
                        "payload_sha256": str(candidate_head["payload_digest"]),
                    },
                    "json_pointer": "/objections/0",
                },
            ),
        )
        lease = self.store.reissue_writer_lease(attest_current_principal())
        with self.store.direct_checkpoint_write_scope():
            with self.assertRaisesRegex(
                WorkspaceIntegrityError,
                "does not resolve in its exact owner",
            ):
                self._commit_checkpoint(
                    authority,
                    checkpoint,
                    lease=lease,
                    command_id="checkpoint.v2-fabricated-pointer",
                )

    def test_store_rejects_stale_v2_unresolved_owner_head_atomically(self) -> None:
        authority = self._bind()
        candidate = self._candidate_payload(authority, "candidate.stale-pointer")
        candidate["objections"] = ["The first revision owns this objection."]
        self._commit_candidate(
            authority,
            candidate,
            command_id="candidate.stale-pointer.1",
        )
        stale_head = self.store.list_mission_candidate_heads("mission.1")[0]
        stale_owner_ref = {
            "kind": "candidate",
            "identity": "candidate.stale-pointer",
            "revision": int(stale_head["revision"]),
            "payload_sha256": str(stale_head["payload_digest"]),
        }

        successor = deep_thaw(candidate)
        successor["objections"] = ["The current revision owns a different objection."]
        self.store.commit_candidate_revision(
            executive_epoch_id=authority.executive_epoch_id,
            mission_id="mission.1",
            candidate_id="candidate.stale-pointer",
            payload=successor,
            expected_head_revision=int(stale_head["revision"]),
            expected_head_payload_digest=str(stale_head["payload_digest"]),
            dependency_heads={},
            a1_requirement=None,
            lease=self.store.reissue_writer_lease(attest_current_principal()),
            command_id="candidate.stale-pointer.2",
            actor="direct-store-guard-test",
            expected_canonical_authority_digest=(
                authority.canonical_authority_digest
            ),
        )
        checkpoint = self._v2_checkpoint(
            authority,
            unresolved_pointers=(
                {
                    "owner_ref": stale_owner_ref,
                    "json_pointer": "/objections/0",
                },
            ),
        )
        before_metadata = deep_thaw(self.store.read_metadata())
        before_events = deep_thaw(
            self.store.read_active_executive_epoch("mission.1")
        )
        lease = self.store.reissue_writer_lease(attest_current_principal())

        with self.store.direct_checkpoint_write_scope():
            with self.assertRaisesRegex(
                WorkspaceIntegrityError,
                "not a current owner head",
            ):
                self._commit_checkpoint(
                    authority,
                    checkpoint,
                    lease=lease,
                    command_id="checkpoint.v2-stale-unresolved-owner",
                )
        self.assertIsNone(
            self.store.read_continuation_checkpoint(
                checkpoint_id=str(checkpoint.document["checkpoint_id"])
            )
        )
        self.assertEqual(before_metadata, deep_thaw(self.store.read_metadata()))
        self.assertEqual(
            before_events,
            deep_thaw(self.store.read_active_executive_epoch("mission.1")),
        )

    def test_store_rejects_noncanonical_or_unrelated_v2_unresolved_pointer(self) -> None:
        authority = self._bind()
        candidate = self._candidate_payload(authority, "candidate.pointer-grammar")
        candidate["objections"] = ["One exact objection."]
        self._commit_candidate(
            authority,
            candidate,
            command_id="candidate.pointer-grammar",
        )
        head = self.store.list_mission_candidate_heads("mission.1")[0]
        owner_ref = {
            "kind": "candidate",
            "identity": "candidate.pointer-grammar",
            "revision": int(head["revision"]),
            "payload_sha256": str(head["payload_digest"]),
        }
        for ordinal, pointer in enumerate(
            ("/standing/status", "/objections/00", "/objections/\u0660"),
            start=1,
        ):
            with self.subTest(pointer=pointer):
                checkpoint = self._v2_checkpoint(
                    authority,
                    unresolved_pointers=(
                        {"owner_ref": owner_ref, "json_pointer": pointer},
                    ),
                )
                lease = self.store.reissue_writer_lease(attest_current_principal())
                with self.store.direct_checkpoint_write_scope():
                    with self.assertRaisesRegex(
                        WorkspaceIntegrityError,
                        "not an authored unresolved field",
                    ):
                        self._commit_checkpoint(
                            authority,
                            checkpoint,
                            lease=lease,
                            command_id=f"checkpoint.v2-pointer-grammar.{ordinal}",
                        )

    def test_store_rejects_v2_checkpoint_with_stale_strategy_root(self) -> None:
        authority = self._bind()
        checkpoint = self._v2_checkpoint(authority)
        self._advance_strategy(
            authority,
            command_id="strategy.before-stale-v2-checkpoint",
        )
        stale_document = deep_thaw(checkpoint.document)
        stale_document["project_commit"] = int(
            self.store.read_metadata()["current_project_commit"]
        ) + 1
        checkpoint = DirectContinuationCheckpoint(
            stale_document,
            _digest(stale_document),
        )
        lease = self.store.reissue_writer_lease(attest_current_principal())
        with self.store.direct_checkpoint_write_scope():
            with self.assertRaisesRegex(
                StaleCommandError,
                "unique current Mission Strategy",
            ):
                self._commit_checkpoint(
                    authority,
                    checkpoint,
                    lease=lease,
                    command_id="checkpoint.v2-stale-strategy",
                )

    def _candidate_payload(self, authority, candidate_id: str):
        return deep_thaw(
            prepare_candidate_revision(
                authority=authority,
                candidate_id=candidate_id,
                proposal_kind="mathematical_statement",
                exact_statement="A bounded diagnostic statement remains open.",
                standing={
                    "status": "open",
                    "basis": "Focused Store-boundary fixture.",
                },
            ).document
        )

    def _commit_candidate(self, authority, payload, *, command_id: str):
        return self.store.commit_candidate_revision(
            executive_epoch_id=authority.executive_epoch_id,
            mission_id="mission.1",
            candidate_id=str(payload["candidate_id"]),
            payload=payload,
            expected_head_revision=None,
            expected_head_payload_digest=None,
            dependency_heads={},
            a1_requirement=None,
            lease=self.store.reissue_writer_lease(attest_current_principal()),
            command_id=command_id,
            actor="direct-store-guard-test",
            expected_canonical_authority_digest=(
                authority.canonical_authority_digest
            ),
        )

    def test_store_binds_native_candidate_provenance_but_preserves_formal_route(self) -> None:
        authority = self._bind()

        wrong_epoch = self._candidate_payload(authority, "candidate.wrong-epoch")
        wrong_epoch["provenance"]["executive_epoch_id"] = "epoch.other"
        with self.assertRaisesRegex(
            StaleCommandError,
            "native provenance differs",
        ):
            self._commit_candidate(
                authority,
                wrong_epoch,
                command_id="candidate.wrong-epoch-bypass",
            )

        wrong_authority = self._candidate_payload(
            authority,
            "candidate.wrong-authority",
        )
        wrong_authority["provenance"][
            "executive_epoch_authority_sha256"
        ] = "0" * 64
        with self.assertRaisesRegex(
            StaleCommandError,
            "native provenance differs",
        ):
            self._commit_candidate(
                authority,
                wrong_authority,
                command_id="candidate.wrong-authority-bypass",
            )

        formal = self._candidate_payload(authority, "candidate.formal-route")
        formal["provenance"] = {
            "kind": "formal_attempt",
            "session_ref": {
                "session_id": "session.formal-route",
                "revision": 1,
                "digest_sha256": "1" * 64,
            },
            "attempt_ref": {
                "attempt_id": "attempt.formal-route",
                "digest_sha256": "2" * 64,
            },
        }
        outcome = self._commit_candidate(
            authority,
            formal,
            command_id="candidate.formal-route",
        )
        self.assertFalse(outcome.replayed)
        persisted = self.store.read_candidate_revision(
            mission_id="mission.1",
            candidate_id="candidate.formal-route",
        )
        self.assertEqual(persisted["payload"]["provenance"], formal["provenance"])
        self.assertEqual(
            self.store.read_candidate_author_root_thread(
                CandidateA1Ref(
                    "mission.1",
                    "candidate.formal-route",
                    1,
                    persisted["payload_digest"],
                )
            ),
            "goal-thread:store-guards",
        )


if __name__ == "__main__":
    unittest.main()
