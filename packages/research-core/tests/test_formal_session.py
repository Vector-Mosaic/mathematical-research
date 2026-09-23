from __future__ import annotations

import copy
import hashlib
import json
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Mapping, Sequence
from unittest import mock


PACKAGE_ROOT = Path(__file__).resolve().parents[1]
TEST_ROOT = Path(__file__).resolve().parent
REPO_ROOT = Path(__file__).resolve().parents[3]
for value in (PACKAGE_ROOT, TEST_ROOT):
    if str(value) not in sys.path:
        sys.path.insert(0, str(value))

from research_core.canonical_snapshot import load_canonical_snapshot  # noqa: E402
import research_core.workspace_store as workspace_store_module  # noqa: E402
from research_core.context_revision import (  # noqa: E402
    PersistedContextRevision,
    commit_context_revision,
    issue_context_revision,
    read_context_revision,
)
from research_core.evidence_store import EvidenceCAS  # noqa: E402
from research_core.formal_session import (  # noqa: E402
    PersistedFormalSessionRevision,
    VerifiedFormalSessionResult,
    _issue_verified_formal_session_result,
    _terminal_dependency_heads,
    commit_formal_session_creation,
    discover_formal_requests,
    prepare_formal_session_creation,
    read_formal_session_revision,
    terminate_formal_session,
)
from research_core.json_support import canonical_json_bytes  # noqa: E402
from research_core.mission_evidence import (  # noqa: E402
    RawCaptureArtifactInput,
    canonical_raw_capture_id,
    commit_raw_capture,
    prepare_raw_capture,
)
from research_core.mission_executive import (  # noqa: E402
    DirectExecutiveEpochAuthority,
    OwnerRevisionRef,
    prepare_direct_executive_epoch_authorized_event,
    prepare_direct_executive_epoch_bound_event,
    reissue_direct_executive_epoch_authority,
)
from research_core.mission_frontier import (  # noqa: E402
    FRONTIER_SCHEMA_VERSION,
    PersistedStrategyRevision,
    StrategyRevision,
    commit_strategy_revision,
    prepare_strategy_revision,
    read_mission_strategy_head,
)
from research_core.research_model import deep_thaw  # noqa: E402
from research_core.workspace_paths import WorkspacePaths  # noqa: E402
from research_core.workspace_store import (  # noqa: E402
    IdentityKind,
    RevisionRef,
    StaleCommandError,
    TypedWorkspaceId,
    WorkspaceIntegrityError,
    WorkspaceStore,
)


MISSION_ID = "mission.1"
STRATEGY_ID = "strategy.theta.1"
CONTEXT_ID = "context.formal.theta.1"


def _direct_genesis_fixture() -> dict[str, object]:
    seed = json.loads(
        (
            REPO_ROOT
            / "contracts"
            / "rh_autonomous_mission_seed.v1.json"
        ).read_text(encoding="utf-8")
    )
    seed["project_id"] = "project.rh"
    seed["authority"]["project_id"] = "project.rh"
    mission = seed["mission"]
    mission["project_id"] = "project.rh"
    mission["mission_id"] = MISSION_ID
    mission["strategy_ids"] = [STRATEGY_ID]
    branch = seed["opening_branch"]
    branch["project_id"] = "project.rh"
    branch["mission_id"] = MISSION_ID
    strategy = seed["strategy"]
    strategy["project_id"] = "project.rh"
    strategy["mission_id"] = MISSION_ID
    strategy["strategy_id"] = STRATEGY_ID
    branch_digest = _digest(branch)

    def rebind_references(value: object) -> None:
        if isinstance(value, dict):
            if set(value) == {
                "kind",
                "identity",
                "revision",
                "payload_sha256",
            }:
                value["payload_sha256"] = branch_digest
                return
            for child in value.values():
                rebind_references(child)
        elif isinstance(value, list):
            for child in value:
                rebind_references(child)

    rebind_references(strategy)
    return seed


def _digest(value: Any) -> str:
    raw = value if isinstance(value, bytes) else canonical_json_bytes(value)
    return hashlib.sha256(raw).hexdigest()


def _event_chain(
    *,
    mission_digest: str = "1" * 64,
    epoch_id: str = "epoch.formal.1",
) -> tuple[Mapping[str, Any], Mapping[str, Any]]:
    authorized = prepare_direct_executive_epoch_authorized_event(
        executive_epoch_id=epoch_id,
        mission_root=OwnerRevisionRef(
            "mission",
            MISSION_ID,
            1,
            mission_digest,
        ),
        predecessor_checkpoint=None,
    )
    bound = prepare_direct_executive_epoch_bound_event(
        executive_epoch_id=epoch_id,
        mission_id=MISSION_ID,
        goal_thread_id=f"thread:{epoch_id}",
        workspace_root="C:/work/rh",
    )

    def readback(event: Any, ordinal: int, predecessor: Any) -> Mapping[str, Any]:
        return {
            "executive_epoch_id": event.executive_epoch_id,
            "event_ordinal": ordinal,
            "mission_id": event.mission_id,
            "project_commit_no": ordinal,
            "event_kind": event.event_kind,
            "event": deep_thaw(event.document),
            "event_digest": event.digest_sha256,
            "predecessor_event_ordinal": None if predecessor is None else 1,
            "predecessor_event_digest": (
                None if predecessor is None else predecessor.digest_sha256
            ),
            "created_actor": "coordinating-codex",
            "created_at": f"2026-08-24T12:00:0{ordinal}Z",
            "row_digest": str(ordinal) * 64,
        }

    return (
        readback(authorized, 1, None),
        readback(bound, 2, authorized),
    )


def _authority(
    *,
    mission_digest: str = "1" * 64,
    epoch_id: str = "epoch.formal.1",
    root_identity: str = "root.rh",
    canonical_authority_digest: str = "c" * 64,
) -> DirectExecutiveEpochAuthority:
    return reissue_direct_executive_epoch_authority(
        event_readbacks=_event_chain(
            mission_digest=mission_digest,
            epoch_id=epoch_id,
        ),
        project_id="project.rh",
        root_identity=root_identity,
        canonical_authority_digest=canonical_authority_digest,
    )


def _context_ref(context: PersistedContextRevision) -> Mapping[str, Any]:
    return {
        "kind": "context",
        "identity": context.record.document["context_id"],
        "revision": context.revision,
        "payload_sha256": context.payload_digest,
    }


def _strategy_payload(
    *,
    context_ref: Mapping[str, Any],
    include_formal: bool = True,
    revision: int = 1,
    formal_bet: str = "falsify the normalized transfer identity",
    formal_discriminator: str = (
        "one exact counterexample rejects the transfer mechanism"
    ),
) -> PersistedStrategyRevision:
    selected_bets: list[Mapping[str, Any]] = [
        {
            "bet": "inspect the current identity directly",
            "discriminator": "the direct calculation either exposes a gap or does not",
            "owner_refs": [],
        }
    ]
    if include_formal:
        selected_bets.append(
            {
                "bet": formal_bet,
                "discriminator": formal_discriminator,
                "owner_refs": [],
                "formal_request": {
                    "purpose": "targeted_falsification",
                    "context_ref": context_ref,
                },
            }
        )
    document = {
        "schema_version": FRONTIER_SCHEMA_VERSION,
        "kind": "mission_strategy",
        "project_id": "project.rh",
        "mission_id": MISSION_ID,
        "strategy_id": STRATEGY_ID,
        "mission_continuation": "continue",
        "integrated_comparison": (
            "The exact transfer falsifier is separate from ordinary native inspection."
        ),
        "causal_inputs": [],
        "serious_opportunities": [],
        "selected_bets": sorted(selected_bets, key=canonical_json_bytes),
        "attention_actions": [],
        "context_treatment": [],
        "creativity_treatment": [],
        "reconsideration_conditions": [],
        "reversal_conditions": [],
        "revival_conditions": [],
        "owner_refs": [],
    }
    record = StrategyRevision(document=document, payload_sha256=_digest(document))
    return PersistedStrategyRevision(
        record=record,
        revision=revision,
        predecessor_revision=None if revision == 1 else revision - 1,
        created_actor="coordinating-codex",
        created_at=f"strategy-revision-{revision}",
    )


def _stored_strategy(strategy: PersistedStrategyRevision) -> Mapping[str, Any]:
    return {
        "strategy_id": STRATEGY_ID,
        "mission_id": MISSION_ID,
        "revision": strategy.revision,
        "payload": deep_thaw(strategy.record.document),
        "payload_digest": strategy.record.payload_sha256,
        "predecessor_revision": strategy.predecessor_revision,
        "created_actor": strategy.created_actor,
        "created_at": strategy.created_at,
    }


def _stored_context(
    context: PersistedContextRevision,
) -> Mapping[str, Any]:
    return {
        "context_id": CONTEXT_ID,
        "revision": context.revision,
        "payload": deep_thaw(context.record.document),
        "payload_digest": context.payload_digest,
        "predecessor_revision": context.predecessor_revision,
        "created_actor": context.created_actor,
        "created_at": context.created_at,
    }


class _FormalSessionStoreFake:
    def __init__(self) -> None:
        self.project_id = "project.rh"
        self.root_identity = "root.rh"
        self.canonical_authority_digest = "c" * 64
        self.mission_digest = "1" * 64
        self.authority = _authority()
        self.event_readbacks = _event_chain()
        self.mission_head = SimpleNamespace(
            reference=SimpleNamespace(revision=1),
            payload_digest=self.mission_digest,
        )
        mission_ref = deep_thaw(self.authority.mission_root)
        context_record = issue_context_revision(
            authority=self.authority,
            context_id=CONTEXT_ID,
            purpose="Freeze the exact transfer falsifier for formal execution.",
            question="Does the normalized transfer identity survive its exact test?",
            indispensable_ground=(
                {
                    "reference": mission_ref,
                    "why": "it fixes the Mission whose formal question is being tested",
                },
            ),
            owner_source_references=(
                {
                    "reference": mission_ref,
                    "retrieval": "direct current Mission owner revision",
                    "provenance": "active Executive Epoch Mission root",
                },
            ),
            known_omissions=("unrelated proof programs",),
            restricted_uses=("do not infer RH from provider completion",),
            independence_treatment={"method": "targeted falsification"},
            restrictions=("raw output has no mathematical authority",),
            invalidation_conditions=(
                {
                    "reference": mission_ref,
                    "condition": "the active Mission root changes",
                },
            ),
        )
        self.context = PersistedContextRevision(
            record=context_record,
            revision=1,
            payload_digest=context_record.digest_sha256,
            predecessor_revision=None,
            created_actor="coordinating-codex",
            created_at="context-revision-1",
            is_current_head=True,
        )
        self.strategy = _strategy_payload(context_ref=_context_ref(self.context))
        self.context_history = {self.context.revision: self.context}
        self.strategy_history = {self.strategy.revision: self.strategy}
        self.sessions: dict[str, list[Mapping[str, Any]]] = {}
        self.raw_captures: dict[str, Mapping[str, Any]] = {}
        self.commit_calls: list[Mapping[str, Any]] = []

    def read_metadata(self) -> Mapping[str, Any]:
        return {
            "project_id": self.project_id,
            "root_identity": self.root_identity,
            "canonical_authority_digest": self.canonical_authority_digest,
        }

    def read_active_executive_epoch(
        self, mission_id: str
    ) -> tuple[Mapping[str, Any], Mapping[str, Any]] | None:
        if mission_id != MISSION_ID:
            return None
        return copy.deepcopy(self.event_readbacks)

    def get_head(self, object_id: TypedWorkspaceId) -> Any:
        if object_id.kind is IdentityKind.MISSION and object_id.value == MISSION_ID:
            return self.mission_head
        return None

    def get_revision(self, reference: RevisionRef) -> Any:
        if (
            reference.object_id.kind is IdentityKind.MISSION
            and reference.object_id.value == MISSION_ID
            and reference.revision == 1
        ):
            return self.mission_head
        return None

    def read_mission_strategy_head(self, mission_id: str) -> Mapping[str, Any]:
        if mission_id != MISSION_ID:
            raise StaleCommandError("Strategy absent")
        return copy.deepcopy(_stored_strategy(self.strategy))

    def read_strategy_revision(
        self,
        *,
        mission_id: str,
        strategy_id: str,
        revision: int | None = None,
    ) -> Mapping[str, Any]:
        if mission_id != MISSION_ID or strategy_id != STRATEGY_ID:
            raise StaleCommandError("Strategy absent")
        selected = (
            self.strategy if revision is None else self.strategy_history.get(revision)
        )
        if selected is None:
            raise StaleCommandError("Strategy revision absent")
        return copy.deepcopy(_stored_strategy(selected))

    def read_context_revision(
        self, context_id: str, revision: int | None = None
    ) -> Mapping[str, Any]:
        if context_id != CONTEXT_ID:
            raise StaleCommandError("Context absent")
        selected = (
            self.context if revision is None else self.context_history.get(revision)
        )
        if selected is None:
            raise StaleCommandError("Context revision absent")
        return copy.deepcopy(_stored_context(selected))

    def read_formal_session_revision(
        self,
        *,
        mission_id: str,
        session_id: str,
        revision: int | None = None,
    ) -> Mapping[str, Any]:
        if mission_id != MISSION_ID or not self.sessions.get(session_id):
            raise StaleCommandError("Session absent")
        rows = self.sessions[session_id]
        if revision is None:
            return copy.deepcopy(rows[-1])
        if revision < 1 or revision > len(rows):
            raise StaleCommandError("Session revision absent")
        return copy.deepcopy(rows[revision - 1])

    def commit_formal_session_revision(self, **kwargs: Any) -> Any:
        recorded = dict(kwargs)
        recorded["payload"] = deep_thaw(kwargs["payload"])
        recorded["dependency_heads"] = dict(kwargs["dependency_heads"])
        self.commit_calls.append(recorded)
        session_id = str(kwargs["session_id"])
        history = self.sessions.setdefault(session_id, [])
        current = None if not history else history[-1]
        payload = deep_thaw(kwargs["payload"])
        payload_digest = _digest(payload)
        if current is not None and current["payload_digest"] == payload_digest:
            return SimpleNamespace(
                replayed=True,
                project_commit=len(history),
                changed_heads=(),
            )
        expected = (
            kwargs["expected_head_revision"],
            kwargs["expected_head_payload_digest"],
        )
        actual = (
            None if current is None else current["revision"],
            None if current is None else current["payload_digest"],
        )
        if expected != actual:
            raise StaleCommandError("Session target stale")
        revision = 1 if current is None else int(current["revision"]) + 1
        history.append(
            {
                "session_id": session_id,
                "mission_id": kwargs["mission_id"],
                "revision": revision,
                "payload": payload,
                "payload_digest": payload_digest,
                "predecessor_revision": None if revision == 1 else revision - 1,
                "created_actor": kwargs["actor"],
                "created_at": f"session-revision-{revision}",
            }
        )
        return SimpleNamespace(
            replayed=False,
            project_commit=len(history),
            changed_heads=(("session", session_id, revision),),
        )

    def seed_output_capture(self, session_id: str) -> Mapping[str, Any]:
        observation_id = f"observation:{session_id}"
        capture_id = canonical_raw_capture_id(
            project_id=self.project_id,
            capture_kind="output",
            observation_id=observation_id,
        )
        artifact = {
            "ordinal": 0,
            "role": "worker_output",
            "logical_name": "worker-output.txt",
            "blob_sha256": _digest(b"exact worker output"),
        }
        payload = {
            "capture_id": capture_id,
            "project_id": self.project_id,
            "mission_id": MISSION_ID,
            "executive_epoch_id": self.authority.executive_epoch_id,
            "capture_kind": "output",
            "observation_id": observation_id,
            "assignment_id": session_id,
            "provenance": {"kind": "formal_attempt_result"},
            "completion": {"state": "returned"},
            "artifacts": [artifact],
        }
        self.raw_captures[capture_id] = {
            "record": {
                key: value for key, value in payload.items() if key != "artifacts"
            }
            | {"created_at": "capture-created-at"},
            "artifacts": [artifact],
        }
        return {
            "kind": "raw_capture",
            "capture_id": capture_id,
            "capture_digest_sha256": _digest(payload),
        }

    def read_raw_capture(self, capture_id: str) -> Mapping[str, Any]:
        if capture_id not in self.raw_captures:
            raise StaleCommandError("raw capture absent")
        return copy.deepcopy(self.raw_captures[capture_id])


def _formal_request(store: _FormalSessionStoreFake) -> Any:
    strategy = read_mission_strategy_head(store, mission_id=MISSION_ID)
    requests = discover_formal_requests(strategy)
    if len(requests) != 1:
        raise AssertionError("fixture must expose exactly one formal request")
    return requests[0]


def _prepare_creation(
    store: _FormalSessionStoreFake,
    *,
    formal_request: Any | None = None,
) -> Any:
    return prepare_formal_session_creation(
        store,
        authority=store.authority,
        formal_request=(
            _formal_request(store) if formal_request is None else formal_request
        ),
    )


def _verified_result(
    store: _FormalSessionStoreFake,
    *,
    session_id: str,
    attempt_state: str = "succeeded",
    result_digest_sha256: str = "d" * 64,
    capture_output: bool = True,
) -> VerifiedFormalSessionResult:
    capture_ref = store.seed_output_capture(session_id) if capture_output else None
    return _issue_verified_formal_session_result(
        session_id=session_id,
        attempt_id="attempt.formal.theta.1",
        attempt_state=attempt_state,
        result_digest_sha256=result_digest_sha256,
        raw_capture_id=(
            None if capture_ref is None else str(capture_ref["capture_id"])
        ),
        raw_capture_digest_sha256=(
            None if capture_ref is None else str(capture_ref["capture_digest_sha256"])
        ),
    )


class FormalSessionContractTests(unittest.TestCase):
    def test_discovery_returns_only_actual_formal_requests(self) -> None:
        store = _FormalSessionStoreFake()
        strategy = read_mission_strategy_head(store, mission_id=MISSION_ID)
        requests = discover_formal_requests(strategy)
        self.assertEqual(len(requests), 1)
        selected = requests[0]
        self.assertEqual(selected.purpose, "targeted_falsification")
        self.assertEqual(
            selected.selected_bet_sha256,
            _digest(selected.selected_bet),
        )
        self.assertEqual(selected.strategy_ref, strategy.to_reference())
        self.assertEqual(selected.context_ref, _context_ref(store.context))

        ordinary = next(
            bet
            for bet in strategy.record.document["selected_bets"]
            if "formal_request" not in bet
        )
        with self.assertRaisesRegex(
            ValueError,
            "selected_bet must be one exact Strategy formal request",
        ):
            type(selected)(
                strategy_ref=selected.strategy_ref,
                selected_bet_sha256=_digest(ordinary),
                selected_bet=ordinary,
            )
        self.assertEqual(store.sessions, {})
        self.assertEqual(store.commit_calls, [])

    def test_creation_records_only_the_closed_semantic_request(self) -> None:
        store = _FormalSessionStoreFake()
        prepared = _prepare_creation(store)
        outcome = commit_formal_session_creation(
            store,
            authority=store.authority,
            prepared=prepared,
            lease=object(),
            actor="coordinating-codex",
        )
        self.assertFalse(outcome.replayed)
        session_id = str(prepared.record.document["session_id"])
        self.assertRegex(session_id, r"^session\.formal\.[0-9a-f]{64}$")
        persisted = read_formal_session_revision(
            store,
            mission_id=MISSION_ID,
            session_id=session_id,
        )
        self.assertIs(type(persisted), PersistedFormalSessionRevision)
        self.assertEqual(persisted.revision, 1)
        self.assertEqual(persisted.record.document["lifecycle"], "open")
        self.assertEqual(
            persisted.record.semantic_identity_sha256,
            session_id.removeprefix("session.formal."),
        )
        self.assertEqual(
            persisted.record.document["selected_bet_sha256"],
            _formal_request(store).selected_bet_sha256,
        )
        self.assertEqual(
            persisted.record.document["context_ref"],
            _context_ref(store.context),
        )
        self.assertEqual(persisted.record.document["terminal_binding"], None)
        self.assertEqual(
            store.commit_calls[0]["dependency_heads"],
            {
                f"mission:{MISSION_ID}": (1, store.mission_digest),
                f"strategy:{STRATEGY_ID}": (
                    store.strategy.revision,
                    store.strategy.record.payload_sha256,
                ),
                f"context:{CONTEXT_ID}": (
                    store.context.revision,
                    store.context.payload_digest,
                ),
            },
        )

        def all_keys(value: Any) -> set[str]:
            if isinstance(value, Mapping):
                return set(value) | set().union(
                    *(all_keys(item) for item in value.values())
                )
            if isinstance(value, Sequence) and not isinstance(
                value, (str, bytes, bytearray)
            ):
                return set().union(*(all_keys(item) for item in value))
            return set()

        forbidden = {
            "budget",
            "resource",
            "resource_request",
            "token_budget",
            "time_limit",
            "attempt_count",
            "max_attempts",
            "byte_limit",
            "deadline",
            "settlement",
            "reservation",
            "selected_bet",
            "expected_raw_output_roles",
            "invalidation_conditions",
            "contextual_retry",
        }
        self.assertFalse(forbidden & all_keys(persisted.record.document))

    def test_changed_strategy_or_context_fails_before_store_write(self) -> None:
        strategy_store = _FormalSessionStoreFake()
        prepared = _prepare_creation(strategy_store)
        strategy_store.strategy = _strategy_payload(
            context_ref=_context_ref(strategy_store.context),
            include_formal=False,
            revision=2,
        )
        with self.assertRaises(StaleCommandError):
            commit_formal_session_creation(
                strategy_store,
                authority=strategy_store.authority,
                prepared=prepared,
                lease=object(),
                actor="coordinating-codex",
            )
        self.assertEqual(strategy_store.commit_calls, [])

        context_store = _FormalSessionStoreFake()
        prepared = _prepare_creation(context_store)
        revised_record = issue_context_revision(
            authority=context_store.authority,
            context_id=CONTEXT_ID,
            purpose=context_store.context.record.document["purpose"],
            question="Which changed exact domain now governs the falsifier?",
            indispensable_ground=context_store.context.record.document[
                "indispensable_ground"
            ],
            owner_source_references=context_store.context.record.document[
                "owner_source_references"
            ],
            known_omissions=context_store.context.record.document["known_omissions"],
            restricted_uses=context_store.context.record.document["restricted_uses"],
            independence_treatment=context_store.context.record.document[
                "independence_treatment"
            ],
            restrictions=context_store.context.record.document["restrictions"],
            invalidation_conditions=context_store.context.record.document[
                "invalidation_conditions"
            ],
        )
        context_store.context = PersistedContextRevision(
            record=revised_record,
            revision=2,
            payload_digest=revised_record.digest_sha256,
            predecessor_revision=1,
            created_actor="coordinating-codex",
            created_at="context-revision-2",
            is_current_head=True,
        )
        with self.assertRaises(StaleCommandError):
            commit_formal_session_creation(
                context_store,
                authority=context_store.authority,
                prepared=prepared,
                lease=object(),
                actor="coordinating-codex",
            )
        self.assertEqual(context_store.commit_calls, [])

    def test_deterministic_identity_replays_and_changes_with_request_revisions(
        self,
    ) -> None:
        store = _FormalSessionStoreFake()
        first_request = _formal_request(store)
        created = _prepare_creation(store, formal_request=first_request)
        session_id = str(created.record.document["session_id"])
        commit_formal_session_creation(
            store,
            authority=store.authority,
            prepared=created,
            lease=object(),
            actor="coordinating-codex",
        )

        # A later Strategy revision changes identity even when the selected bet
        # and Context bytes are otherwise unchanged.
        changed_strategy = _strategy_payload(
            context_ref=_context_ref(store.context),
            revision=2,
        )
        store.strategy = changed_strategy
        store.strategy_history[changed_strategy.revision] = changed_strategy
        second_request = _formal_request(store)
        second = _prepare_creation(store, formal_request=second_request)
        second_session_id = str(second.record.document["session_id"])
        self.assertNotEqual(session_id, second_session_id)

        # The exact first request still finds its deterministic owner before
        # current-Strategy validation, so a lost creation response is a no-op.
        replay_prepared = _prepare_creation(store, formal_request=first_request)
        self.assertEqual(replay_prepared.record.document["session_id"], session_id)
        replay_outcome = commit_formal_session_creation(
            store,
            authority=store.authority,
            prepared=replay_prepared,
            lease=object(),
            actor="coordinating-codex",
            command_id="formal-session-creation-lost-response",
        )
        self.assertTrue(replay_outcome.replayed)
        self.assertEqual(len(store.sessions), 1)
        self.assertEqual(len(store.sessions[session_id]), 1)

        context_record = issue_context_revision(
            authority=store.authority,
            context_id=CONTEXT_ID,
            purpose=store.context.record.document["purpose"],
            question="Which later Context should guide interpretation after return?",
            indispensable_ground=store.context.record.document["indispensable_ground"],
            owner_source_references=store.context.record.document[
                "owner_source_references"
            ],
            known_omissions=store.context.record.document["known_omissions"],
            restricted_uses=store.context.record.document["restricted_uses"],
            independence_treatment=store.context.record.document[
                "independence_treatment"
            ],
            restrictions=store.context.record.document["restrictions"],
            invalidation_conditions=store.context.record.document[
                "invalidation_conditions"
            ],
        )
        changed_context = PersistedContextRevision(
            record=context_record,
            revision=2,
            payload_digest=context_record.digest_sha256,
            predecessor_revision=1,
            created_actor="coordinating-codex",
            created_at="context-revision-2",
            is_current_head=True,
        )
        store.context = changed_context
        store.context_history[changed_context.revision] = changed_context
        context_strategy = _strategy_payload(
            context_ref=_context_ref(changed_context),
            revision=3,
        )
        store.strategy = context_strategy
        store.strategy_history[context_strategy.revision] = context_strategy
        third = _prepare_creation(store, formal_request=_formal_request(store))
        third_session_id = str(third.record.document["session_id"])
        self.assertNotIn(third_session_id, {session_id, second_session_id})

        verified_result = _verified_result(store, session_id=session_id)
        terminal = terminate_formal_session(
            store,
            authority=store.authority,
            verified_result=verified_result,
            lease=object(),
            actor="coordinating-codex",
        )
        self.assertFalse(terminal.replayed)
        self.assertEqual(len(store.sessions[session_id]), 2)
        persisted = read_formal_session_revision(
            store,
            mission_id=MISSION_ID,
            session_id=session_id,
        )
        self.assertEqual(persisted.revision, 2)
        self.assertEqual(persisted.record.document["lifecycle"], "terminal")
        self.assertEqual(
            persisted.record.document["terminal_binding"],
            verified_result.terminal_binding,
        )
        historical = read_formal_session_revision(
            store,
            mission_id=MISSION_ID,
            session_id=session_id,
            revision=1,
        )
        self.assertFalse(historical.is_current_head)
        self.assertEqual(
            historical.record.semantic_identity_sha256,
            persisted.record.semantic_identity_sha256,
        )

        replay = terminate_formal_session(
            store,
            authority=store.authority,
            verified_result=verified_result,
            lease=object(),
            actor="coordinating-codex",
            command_id="formal-session-terminal-replay",
        )
        self.assertTrue(replay.replayed)
        self.assertEqual(len(store.sessions[session_id]), 2)

        changed_result = _verified_result(
            store,
            session_id=session_id,
            result_digest_sha256="e" * 64,
        )
        with self.assertRaisesRegex(ValueError, "already terminal"):
            terminate_formal_session(
                store,
                authority=store.authority,
                verified_result=changed_result,
                lease=object(),
                actor="coordinating-codex",
            )
        self.assertEqual(len(store.sessions[session_id]), 2)

    def test_only_bridge_issued_settleable_results_can_terminalize(self) -> None:
        store = _FormalSessionStoreFake()
        prepared = _prepare_creation(store)
        session_id = str(prepared.record.document["session_id"])
        commit_formal_session_creation(
            store,
            authority=store.authority,
            prepared=prepared,
            lease=object(),
            actor="coordinating-codex",
        )
        forged = object.__new__(VerifiedFormalSessionResult)
        object.__setattr__(forged, "_issuer", object())
        with self.assertRaisesRegex(TypeError, "MR-WC bridge"):
            forged.verify_issued()
        with self.assertRaisesRegex(TypeError, "bridge-issued"):
            terminate_formal_session(
                store,
                authority=store.authority,
                verified_result=object(),  # type: ignore[arg-type]
                lease=object(),
                actor="coordinating-codex",
            )
        with self.assertRaisesRegex(ValueError, "not a settleable terminal fact"):
            _issue_verified_formal_session_result(
                session_id=session_id,
                attempt_id="attempt.unknown",
                attempt_state="unknown",
                result_digest_sha256="f" * 64,
                raw_capture_id=None,
                raw_capture_digest_sha256=None,
            )

        successful_zero_output = _issue_verified_formal_session_result(
            session_id=session_id,
            attempt_id="attempt.succeeded.zero-output",
            attempt_state="succeeded",
            result_digest_sha256="e" * 64,
            raw_capture_id=None,
            raw_capture_digest_sha256=None,
        )
        self.assertIsNone(successful_zero_output.terminal_binding["raw_capture_ref"])

        failed_without_output = _issue_verified_formal_session_result(
            session_id=session_id,
            attempt_id="attempt.failed.no-output",
            attempt_state="failed",
            result_digest_sha256="f" * 64,
            raw_capture_id=None,
            raw_capture_digest_sha256=None,
        )
        terminal = terminate_formal_session(
            store,
            authority=store.authority,
            verified_result=failed_without_output,
            lease=object(),
            actor="coordinating-codex",
        )
        self.assertFalse(terminal.replayed)
        persisted = read_formal_session_revision(
            store,
            mission_id=MISSION_ID,
            session_id=session_id,
        )
        self.assertEqual(
            persisted.record.document["terminal_binding"],
            failed_without_output.terminal_binding,
        )
        self.assertIsNone(
            persisted.record.document["terminal_binding"]["raw_capture_ref"]
        )

    def test_real_store_never_reads_the_aggregate_origin_and_only_session_advances(
        self,
    ) -> None:
        loaded = load_canonical_snapshot(REPO_ROOT, source_commit="a" * 40)
        self.assertTrue(loaded.ok, loaded.failure)
        assert loaded.value is not None
        with tempfile.TemporaryDirectory() as temporary:
            store = WorkspaceStore.initialize_direct_mission_workspace(
                WorkspacePaths.from_root(Path(temporary) / "workspace"),
                project_id="project.rh",
                canonical_snapshot=loaded.value,
                actor="formal-session-test",
            )
            metadata = store.read_metadata()
            authority_digest = str(metadata["canonical_authority_digest"])
            lease = store.claim_writer(
                owner="formal-session-test",
                creation_basis="focused-formal-session-integration",
                expected_project_commit=int(metadata["current_project_commit"]),
                expected_root_digest=str(metadata["current_root_digest"]),
                expected_canonical_authority_digest=authority_digest,
            )
            store.initialize_direct_mission_genesis(
                _direct_genesis_fixture(),
                mission_id=MISSION_ID,
                canonical_snapshot=loaded.value,
                lease=lease,
                command_id="formal.direct-genesis",
                actor="formal-session-test",
            )
            with store.snapshot_connection() as connection:
                self.assertEqual(
                    int(
                        connection.execute(
                            "SELECT COUNT(*) FROM mission_bundle_snapshot"
                        ).fetchone()[0]
                    ),
                    0,
                )
            mission_head = store.get_head(
                TypedWorkspaceId(IdentityKind.MISSION, MISSION_ID)
            )
            self.assertIsNotNone(mission_head)
            assert mission_head is not None
            epoch_id = "epoch.formal.real-store.1"
            authorized = prepare_direct_executive_epoch_authorized_event(
                executive_epoch_id=epoch_id,
                mission_root=OwnerRevisionRef(
                    "mission",
                    MISSION_ID,
                    mission_head.reference.revision,
                    mission_head.payload_digest,
                ),
                predecessor_checkpoint=None,
            )
            bound = prepare_direct_executive_epoch_bound_event(
                executive_epoch_id=epoch_id,
                mission_id=MISSION_ID,
                goal_thread_id=f"thread:{epoch_id}",
                workspace_root="C:/work/rh",
            )
            authorization_metadata = store.read_metadata()
            store.authorize_executive_epoch(
                mission_id=MISSION_ID,
                executive_epoch_id=epoch_id,
                event=authorized,
                lease=lease,
                command_id="formal.epoch.authorize",
                actor="formal-session-test",
                expected_canonical_authority_digest=authority_digest,
                expected_mission_host_store_cut={
                    "project_commit": int(
                        authorization_metadata["current_project_commit"]
                    ),
                    "current_root_digest": str(
                        authorization_metadata["current_root_digest"]
                    ),
                    "transition_head_digest": authorization_metadata[
                        "transition_head_digest"
                    ],
                    "canonical_authority_digest": str(
                        authorization_metadata["canonical_authority_digest"]
                    ),
                },
            )
            store.bind_executive_epoch(
                mission_id=MISSION_ID,
                executive_epoch_id=epoch_id,
                event=bound,
                expected_event_ordinal=1,
                expected_event_digest=authorized.digest_sha256,
                lease=lease,
                command_id="formal.epoch.bind",
                actor="formal-session-test",
                expected_canonical_authority_digest=authority_digest,
            )
            readbacks = store.read_active_executive_epoch(MISSION_ID)
            self.assertIsNotNone(readbacks)
            assert readbacks is not None
            authority = reissue_direct_executive_epoch_authority(
                event_readbacks=readbacks,
                project_id="project.rh",
                root_identity=store.paths.root_identity,
                canonical_authority_digest=authority_digest,
            )

            initial_strategy = _strategy_payload(
                context_ref={
                    "kind": "context",
                    "identity": "context.not-yet-used",
                    "revision": 1,
                    "payload_sha256": "9" * 64,
                },
                include_formal=False,
            )
            legacy_strategy_head = store.get_head(
                TypedWorkspaceId(IdentityKind.STRATEGY, STRATEGY_ID)
            )
            self.assertIsNotNone(legacy_strategy_head)
            assert legacy_strategy_head is not None
            store.commit_strategy_revision(
                executive_epoch_id=epoch_id,
                mission_id=MISSION_ID,
                strategy_id=STRATEGY_ID,
                payload=initial_strategy.record.document,
                expected_head_revision=legacy_strategy_head.reference.revision,
                expected_head_payload_digest=legacy_strategy_head.payload_digest,
                dependency_heads=None,
                lease=lease,
                command_id="formal.strategy.genesis",
                actor="formal-session-test",
                expected_canonical_authority_digest=authority_digest,
            )
            context_record = issue_context_revision(
                authority=authority,
                context_id=CONTEXT_ID,
                purpose="Freeze one exact formal falsification question.",
                question="Does the normalized transfer identity survive?",
                indispensable_ground=(
                    {
                        "reference": deep_thaw(authority.mission_root),
                        "why": "it fixes the Mission whose formal question is tested",
                    },
                ),
                owner_source_references=(
                    {
                        "reference": deep_thaw(authority.mission_root),
                        "retrieval": "direct current Mission owner revision",
                        "provenance": "active Executive Epoch Mission root",
                    },
                ),
                known_omissions=("unrelated proof programs",),
                restricted_uses=("do not treat raw output as Evidence",),
                independence_treatment={"method": "targeted falsification"},
                restrictions=("no canonical mathematical effect",),
                invalidation_conditions=(
                    {
                        "reference": deep_thaw(authority.mission_root),
                        "condition": "the active Mission root changes",
                    },
                ),
            )
            commit_context_revision(
                store,
                authority=authority,
                record=context_record,
                lease=lease,
                actor="formal-session-test",
            )
            context = read_context_revision(store, context_id=CONTEXT_ID)
            strategy_write = prepare_strategy_revision(
                store,
                project_id="project.rh",
                mission_id=MISSION_ID,
                strategy_id=STRATEGY_ID,
                mission_continuation="continue",
                integrated_comparison=(
                    "The formal falsifier is selected only for exact custody and replay."
                ),
                selected_bets=(
                    {
                        "bet": "falsify the normalized transfer identity",
                        "discriminator": (
                            "one exact counterexample rejects the transfer mechanism"
                        ),
                        "owner_refs": (),
                        "formal_request": {
                            "purpose": "targeted_falsification",
                            "context_ref": _context_ref(context),
                        },
                    },
                    {
                        "bet": "inspect the current identity directly",
                        "discriminator": "the native calculation exposes a gap or not",
                        "owner_refs": (),
                    },
                ),
            )
            commit_strategy_revision(
                store,
                authority=authority,
                prepared=strategy_write,
                lease=lease,
                actor="formal-session-test",
            )
            strategy = read_mission_strategy_head(store, mission_id=MISSION_ID)
            formal_request = discover_formal_requests(strategy)[0]
            before_heads = {
                "mission": (
                    mission_head.reference.revision,
                    mission_head.payload_digest,
                ),
                "strategy": (strategy.revision, strategy.record.payload_sha256),
                "context": (context.revision, context.payload_digest),
            }
            self.assertEqual(store.list_mission_formal_session_heads(MISSION_ID), ())
            self.assertEqual(store.list_mission_evidence_meaning_heads(MISSION_ID), ())

            prepared = prepare_formal_session_creation(
                store,
                authority=authority,
                formal_request=formal_request,
            )
            session_id = str(prepared.record.document["session_id"])

            def attempt_observation_id(attempt: str, digest: str) -> str:
                return (
                    "formal-attempt-result:"
                    + hashlib.sha256(
                        canonical_json_bytes(
                            {
                                "domain": (
                                    "mathematical_research."
                                    "formal_attempt_observation.v1"
                                ),
                                "attempt_id": attempt,
                                "result_digest_sha256": digest,
                            }
                        )
                    ).hexdigest()
                )

            malformed_payloads = []
            extra_field = deep_thaw(prepared.record.document)
            extra_field["receipt"] = "not part of the Session contract"
            malformed_payloads.append(extra_field)
            caller_identity = deep_thaw(prepared.record.document)
            caller_identity["session_id"] = f"{session_id}.caller-authored"
            malformed_payloads.append(caller_identity)
            for malformed in malformed_payloads:
                with self.subTest(malformed=malformed["session_id"]):
                    with self.assertRaisesRegex(
                        WorkspaceIntegrityError,
                        "invalid at the Store boundary",
                    ):
                        store.commit_formal_session_revision(
                            executive_epoch_id=epoch_id,
                            mission_id=MISSION_ID,
                            session_id=str(malformed["session_id"]),
                            payload=malformed,
                            expected_head_revision=None,
                            expected_head_payload_digest=None,
                            dependency_heads=None,
                            lease=lease,
                            command_id="formal.store-bypass.rejected",
                            actor="formal-session-test",
                            expected_canonical_authority_digest=authority_digest,
                        )
            cas = EvidenceCAS(store.paths)
            early_attempt_id = "attempt.formal.before-session-genesis"
            early_result_digest = "5" * 64
            early_capture = prepare_raw_capture(
                store,
                mission_id=MISSION_ID,
                executive_epoch_id=epoch_id,
                capture_kind="output",
                observation_id=attempt_observation_id(
                    early_attempt_id,
                    early_result_digest,
                ),
                assignment_id=session_id,
                provenance={
                    "kind": "workstation_attempt_result",
                    "attempt_id": early_attempt_id,
                    "result_digest_sha256": early_result_digest,
                    "session_id": session_id,
                    "session_digest_sha256": prepared.record.payload_sha256,
                },
                artifacts=(
                    RawCaptureArtifactInput(
                        role="worker_output",
                        logical_name="before-session-genesis.txt",
                        content_bytes=b"output predating its claimed Session",
                        media_type="text/plain",
                        encoding="utf-8",
                    ),
                ),
                completion={
                    "attempt_state": "succeeded",
                    "provider_effect_certainty": "known",
                },
            )
            commit_raw_capture(
                store,
                cas=cas,
                record=early_capture,
                lease=lease,
                actor="formal-session-test",
            )
            refreshed_prepared = prepare_formal_session_creation(
                store,
                authority=authority,
                formal_request=formal_request,
            )
            self.assertEqual(
                refreshed_prepared.record.payload_sha256,
                prepared.record.payload_sha256,
            )
            prepared = refreshed_prepared
            commit_formal_session_creation(
                store,
                authority=authority,
                prepared=prepared,
                lease=lease,
                actor="formal-session-test",
            )

            attempt_id = "attempt.formal.real.1"
            result_digest = "8" * 64
            observation_id = attempt_observation_id(attempt_id, result_digest)
            capture = prepare_raw_capture(
                store,
                mission_id=MISSION_ID,
                executive_epoch_id=epoch_id,
                capture_kind="output",
                observation_id=observation_id,
                assignment_id=session_id,
                provenance={
                    "kind": "workstation_attempt_result",
                    "attempt_id": attempt_id,
                    "result_digest_sha256": result_digest,
                    "session_id": session_id,
                    "session_digest_sha256": prepared.record.payload_sha256,
                },
                artifacts=(
                    RawCaptureArtifactInput(
                        role="worker_output",
                        logical_name="worker-output.txt",
                        content_bytes=b"exact formal worker output",
                        media_type="text/plain",
                        encoding="utf-8",
                    ),
                ),
                completion={
                    "attempt_state": "succeeded",
                    "provider_effect_certainty": "known",
                },
            )
            commit_raw_capture(
                store,
                cas=cas,
                record=capture,
                lease=lease,
                actor="formal-session-test",
            )
            mismatch_captures = {}
            for label, other_attempt_id, other_result_digest, other_state in (
                (
                    "wrong_attempt",
                    "attempt.formal.real.2",
                    result_digest,
                    "succeeded",
                ),
                ("wrong_result_digest", attempt_id, "6" * 64, "succeeded"),
            ):
                mismatch_observation_id = attempt_observation_id(
                    other_attempt_id,
                    other_result_digest,
                )
                mismatch_capture = prepare_raw_capture(
                    store,
                    mission_id=MISSION_ID,
                    executive_epoch_id=epoch_id,
                    capture_kind="output",
                    observation_id=mismatch_observation_id,
                    assignment_id=session_id,
                    provenance={
                        "kind": "workstation_attempt_result",
                        "attempt_id": other_attempt_id,
                        "result_digest_sha256": other_result_digest,
                        "session_id": session_id,
                        "session_digest_sha256": prepared.record.payload_sha256,
                    },
                    artifacts=(
                        RawCaptureArtifactInput(
                            role="worker_output",
                            logical_name=f"{label}.txt",
                            content_bytes=label.encode("utf-8"),
                            media_type="text/plain",
                            encoding="utf-8",
                        ),
                    ),
                    completion={
                        "attempt_state": other_state,
                        "provider_effect_certainty": "known",
                    },
                )
                commit_raw_capture(
                    store,
                    cas=cas,
                    record=mismatch_capture,
                    lease=lease,
                    actor="formal-session-test",
                )
                mismatch_captures[label] = mismatch_capture
            isolated_mismatch_captures = {}
            for (
                label,
                variant_attempt_id,
                variant_result_digest,
                observation_override,
                provenance_changes,
                provider_effect_certainty,
            ) in (
                (
                    "wrong_observation_id",
                    "attempt.formal.wrong-observation",
                    "7" * 64,
                    "formal-attempt-result:" + "0" * 64,
                    {},
                    "known",
                ),
                (
                    "wrong_provenance_session",
                    "attempt.formal.wrong-provenance-session",
                    "a" * 64,
                    None,
                    {"session_id": "session.formal.other"},
                    "known",
                ),
                (
                    "wrong_session_digest",
                    "attempt.formal.wrong-session-digest",
                    "b" * 64,
                    None,
                    {"session_digest_sha256": "0" * 64},
                    "known",
                ),
                (
                    "wrong_provider_effect_certainty",
                    "attempt.formal.wrong-provider-effect-certainty",
                    "d" * 64,
                    None,
                    {},
                    "unknown",
                ),
            ):
                variant_observation_id = (
                    attempt_observation_id(
                        variant_attempt_id,
                        variant_result_digest,
                    )
                    if observation_override is None
                    else observation_override
                )
                variant_provenance = {
                    "kind": "workstation_attempt_result",
                    "attempt_id": variant_attempt_id,
                    "result_digest_sha256": variant_result_digest,
                    "session_id": session_id,
                    "session_digest_sha256": prepared.record.payload_sha256,
                    **provenance_changes,
                }
                mismatch_capture = prepare_raw_capture(
                    store,
                    mission_id=MISSION_ID,
                    executive_epoch_id=epoch_id,
                    capture_kind="output",
                    observation_id=variant_observation_id,
                    assignment_id=session_id,
                    provenance=variant_provenance,
                    artifacts=(
                        RawCaptureArtifactInput(
                            role="worker_output",
                            logical_name=f"{label}.txt",
                            content_bytes=label.encode("utf-8"),
                            media_type="text/plain",
                            encoding="utf-8",
                        ),
                    ),
                    completion={
                        "attempt_state": "succeeded",
                        "provider_effect_certainty": provider_effect_certainty,
                    },
                )
                commit_raw_capture(
                    store,
                    cas=cas,
                    record=mismatch_capture,
                    lease=lease,
                    actor="formal-session-test",
                )
                isolated_mismatch_captures[label] = (
                    variant_attempt_id,
                    variant_result_digest,
                    mismatch_capture,
                )
            wrong_assignment_attempt_id = "attempt.formal.wrong-assignment"
            wrong_assignment_result_digest = "4" * 64
            wrong_assignment_capture = prepare_raw_capture(
                store,
                mission_id=MISSION_ID,
                executive_epoch_id=epoch_id,
                capture_kind="output",
                observation_id=attempt_observation_id(
                    wrong_assignment_attempt_id,
                    wrong_assignment_result_digest,
                ),
                assignment_id="session.formal.other",
                provenance={
                    "kind": "workstation_attempt_result",
                    "attempt_id": wrong_assignment_attempt_id,
                    "result_digest_sha256": wrong_assignment_result_digest,
                    "session_id": session_id,
                    "session_digest_sha256": prepared.record.payload_sha256,
                },
                artifacts=(
                    RawCaptureArtifactInput(
                        role="worker_output",
                        logical_name="wrong-assignment.txt",
                        content_bytes=b"valid output for another Session",
                        media_type="text/plain",
                        encoding="utf-8",
                    ),
                ),
                completion={
                    "attempt_state": "succeeded",
                    "provider_effect_certainty": "known",
                },
            )
            commit_raw_capture(
                store,
                cas=cas,
                record=wrong_assignment_capture,
                lease=lease,
                actor="formal-session-test",
            )
            wrong_kind_attempt_id = "attempt.formal.wrong-kind"
            wrong_kind_result_digest = "3" * 64
            wrong_kind_capture = prepare_raw_capture(
                store,
                mission_id=MISSION_ID,
                executive_epoch_id=epoch_id,
                capture_kind="assignment",
                observation_id=attempt_observation_id(
                    wrong_kind_attempt_id,
                    wrong_kind_result_digest,
                ),
                assignment_id=session_id,
                provenance={
                    "kind": "workstation_attempt_result",
                    "attempt_id": wrong_kind_attempt_id,
                    "result_digest_sha256": wrong_kind_result_digest,
                    "session_id": session_id,
                    "session_digest_sha256": prepared.record.payload_sha256,
                },
                artifacts=(
                    RawCaptureArtifactInput(
                        role="assignment",
                        logical_name="assignment.txt",
                        content_bytes=b"valid assignment, not an output",
                        media_type="text/plain",
                        encoding="utf-8",
                    ),
                ),
                completion=None,
            )
            commit_raw_capture(
                store,
                cas=cas,
                record=wrong_kind_capture,
                lease=lease,
                actor="formal-session-test",
            )
            wrong_state_attempt_id = "attempt.formal.wrong-state"
            wrong_state_result_digest = "2" * 64
            wrong_state_capture = prepare_raw_capture(
                store,
                mission_id=MISSION_ID,
                executive_epoch_id=epoch_id,
                capture_kind="output",
                observation_id=attempt_observation_id(
                    wrong_state_attempt_id,
                    wrong_state_result_digest,
                ),
                assignment_id=session_id,
                provenance={
                    "kind": "workstation_attempt_result",
                    "attempt_id": wrong_state_attempt_id,
                    "result_digest_sha256": wrong_state_result_digest,
                    "session_id": session_id,
                    "session_digest_sha256": prepared.record.payload_sha256,
                },
                artifacts=(
                    RawCaptureArtifactInput(
                        role="worker_output",
                        logical_name="wrong-state.txt",
                        content_bytes=b"output with a different terminal state",
                        media_type="text/plain",
                        encoding="utf-8",
                    ),
                ),
                completion={
                    "attempt_state": "failed",
                    "provider_effect_certainty": "known",
                },
            )
            commit_raw_capture(
                store,
                cas=cas,
                record=wrong_state_capture,
                lease=lease,
                actor="formal-session-test",
            )
            verified_result = _issue_verified_formal_session_result(
                session_id=session_id,
                attempt_id=attempt_id,
                attempt_state="succeeded",
                result_digest_sha256=result_digest,
                raw_capture_id=capture.capture_id,
                raw_capture_digest_sha256=capture.digest_sha256,
            )
            terminal_payload = deep_thaw(prepared.record.document)
            terminal_payload["lifecycle"] = "terminal"
            terminal_payload["terminal_binding"] = deep_thaw(
                verified_result.terminal_binding
            )

            def terminal_payload_for_capture(
                *,
                linked_attempt_id: str,
                linked_result_digest: str,
                linked_capture,
            ) -> dict:
                linked_result = _issue_verified_formal_session_result(
                    session_id=session_id,
                    attempt_id=linked_attempt_id,
                    attempt_state="succeeded",
                    result_digest_sha256=linked_result_digest,
                    raw_capture_id=linked_capture.capture_id,
                    raw_capture_digest_sha256=linked_capture.digest_sha256,
                )
                payload = deep_thaw(prepared.record.document)
                payload["lifecycle"] = "terminal"
                payload["terminal_binding"] = deep_thaw(
                    linked_result.terminal_binding
                )
                return payload

            isolated_invalid_terminals = {
                "capture_before_session_genesis": terminal_payload_for_capture(
                    linked_attempt_id=early_attempt_id,
                    linked_result_digest=early_result_digest,
                    linked_capture=early_capture,
                ),
                "wrong_assignment": terminal_payload_for_capture(
                    linked_attempt_id=wrong_assignment_attempt_id,
                    linked_result_digest=wrong_assignment_result_digest,
                    linked_capture=wrong_assignment_capture,
                ),
                "wrong_kind": terminal_payload_for_capture(
                    linked_attempt_id=wrong_kind_attempt_id,
                    linked_result_digest=wrong_kind_result_digest,
                    linked_capture=wrong_kind_capture,
                ),
                "wrong_attempt_state": terminal_payload_for_capture(
                    linked_attempt_id=wrong_state_attempt_id,
                    linked_result_digest=wrong_state_result_digest,
                    linked_capture=wrong_state_capture,
                ),
                **{
                    label: terminal_payload_for_capture(
                        linked_attempt_id=linked_attempt_id,
                        linked_result_digest=linked_result_digest,
                        linked_capture=linked_capture,
                    )
                    for label, (
                        linked_attempt_id,
                        linked_result_digest,
                        linked_capture,
                    ) in isolated_mismatch_captures.items()
                },
            }
            terminal_dependencies = _terminal_dependency_heads(
                store,
                authority=authority,
                document=terminal_payload,
            )
            before_rejected_bindings = dict(store.read_metadata())
            invalid_ref_kind = copy.deepcopy(terminal_payload)
            invalid_ref_kind["terminal_binding"]["raw_capture_ref"]["kind"] = (
                "capture"
            )
            with self.assertRaisesRegex(
                WorkspaceIntegrityError,
                "invalid at the Store boundary",
            ):
                store.commit_formal_session_revision(
                    executive_epoch_id=epoch_id,
                    mission_id=MISSION_ID,
                    session_id=session_id,
                    payload=invalid_ref_kind,
                    expected_head_revision=1,
                    expected_head_payload_digest=prepared.record.payload_sha256,
                    dependency_heads=terminal_dependencies,
                    lease=lease,
                    command_id="formal.invalid-terminal.ref-kind",
                    actor="formal-session-test",
                    expected_canonical_authority_digest=authority_digest,
                )
            self.assertEqual(
                dict(store.read_metadata()),
                before_rejected_bindings,
            )
            invalid_capture_refs = {
                "missing_capture": {
                    "kind": "raw_capture",
                    "capture_id": "capture.missing",
                    "capture_digest_sha256": "9" * 64,
                },
                "stale_digest": {
                    "kind": "raw_capture",
                    "capture_id": capture.capture_id,
                    "capture_digest_sha256": "0" * 64,
                },
                **{
                    label: {
                        "kind": "raw_capture",
                        "capture_id": mismatch_capture.capture_id,
                        "capture_digest_sha256": mismatch_capture.digest_sha256,
                    }
                    for label, mismatch_capture in mismatch_captures.items()
                },
            }
            for label, invalid_ref in invalid_capture_refs.items():
                with self.subTest(invalid_terminal_capture=label):
                    invalid_terminal = copy.deepcopy(terminal_payload)
                    invalid_terminal["terminal_binding"]["raw_capture_ref"] = invalid_ref
                    with self.assertRaisesRegex(
                        WorkspaceIntegrityError,
                        "exact raw Capture|exact prior output custody",
                    ):
                        store.commit_formal_session_revision(
                            executive_epoch_id=epoch_id,
                            mission_id=MISSION_ID,
                            session_id=session_id,
                            payload=invalid_terminal,
                            expected_head_revision=1,
                            expected_head_payload_digest=prepared.record.payload_sha256,
                            dependency_heads=terminal_dependencies,
                            lease=lease,
                            command_id=f"formal.invalid-terminal.{label}",
                            actor="formal-session-test",
                            expected_canonical_authority_digest=authority_digest,
                        )
                    self.assertEqual(
                        dict(store.read_metadata()),
                        before_rejected_bindings,
                    )
            for label, invalid_terminal in isolated_invalid_terminals.items():
                with self.subTest(invalid_terminal_capture=label):
                    with self.assertRaisesRegex(
                        WorkspaceIntegrityError,
                        "exact raw Capture|exact prior output custody",
                    ):
                        store.commit_formal_session_revision(
                            executive_epoch_id=epoch_id,
                            mission_id=MISSION_ID,
                            session_id=session_id,
                            payload=invalid_terminal,
                            expected_head_revision=1,
                            expected_head_payload_digest=prepared.record.payload_sha256,
                            dependency_heads=terminal_dependencies,
                            lease=lease,
                            command_id=f"formal.invalid-terminal.{label}",
                            actor="formal-session-test",
                            expected_canonical_authority_digest=authority_digest,
                        )
                    self.assertEqual(
                        dict(store.read_metadata()),
                        before_rejected_bindings,
                    )
            terminal_command_id = "formal.real.terminal"
            terminal = terminate_formal_session(
                store,
                authority=authority,
                verified_result=verified_result,
                lease=lease,
                actor="formal-session-test",
                command_id=terminal_command_id,
            )
            terminal_exact_replay = store.commit_formal_session_revision(
                executive_epoch_id=epoch_id,
                mission_id=MISSION_ID,
                session_id=session_id,
                payload=terminal_payload,
                expected_head_revision=1,
                expected_head_payload_digest=prepared.record.payload_sha256,
                dependency_heads=terminal_dependencies,
                lease=lease,
                command_id=terminal_command_id,
                actor="formal-session-test",
                expected_canonical_authority_digest=authority_digest,
            )
            terminal_replay = terminate_formal_session(
                store,
                authority=authority,
                verified_result=verified_result,
                lease=lease,
                actor="formal-session-test",
                command_id="formal.real.terminal.replay",
            )

            self.assertFalse(terminal.replayed)
            self.assertTrue(terminal_exact_replay.replayed)
            self.assertEqual(
                terminal_exact_replay.project_commit,
                terminal.project_commit,
            )
            self.assertTrue(terminal_replay.replayed)
            sessions = store.list_mission_formal_session_heads(MISSION_ID)
            self.assertEqual(len(sessions), 1)
            self.assertEqual(sessions[0]["revision"], 2)
            self.assertEqual(sessions[0]["payload"]["lifecycle"], "terminal")
            self.assertEqual(store.list_mission_evidence_meaning_heads(MISSION_ID), ())
            after_mission = store.get_head(
                TypedWorkspaceId(IdentityKind.MISSION, MISSION_ID)
            )
            after_strategy = read_mission_strategy_head(store, mission_id=MISSION_ID)
            after_context = read_context_revision(store, context_id=CONTEXT_ID)
            assert after_mission is not None
            self.assertEqual(
                before_heads,
                {
                    "mission": (
                        after_mission.reference.revision,
                        after_mission.payload_digest,
                    ),
                    "strategy": (
                        after_strategy.revision,
                        after_strategy.record.payload_sha256,
                    ),
                    "context": (
                        after_context.revision,
                        after_context.payload_digest,
                    ),
                },
            )
            with store.snapshot_connection() as connection:
                self.assertEqual(
                    int(
                        connection.execute(
                            "SELECT COUNT(*) FROM mission_bundle_snapshot"
                        ).fetchone()[0]
                    ),
                    0,
                )
            reopened_store = WorkspaceStore.open(store.paths)
            reopened_session = read_formal_session_revision(
                reopened_store,
                mission_id=MISSION_ID,
                session_id=session_id,
            )
            self.assertEqual(reopened_session.revision, 2)
            self.assertEqual(
                reopened_session.record.document["terminal_binding"],
                verified_result.terminal_binding,
            )
            with store.direct_recovery_read_scope():
                retrieval_cut = store.read_root_retrieval_cut()
                terminal_cut = int(retrieval_cut["project_commit"])
            with mock.patch.object(
                workspace_store_module,
                "_require_formal_session_capture_dependency",
                wraps=workspace_store_module._require_formal_session_capture_dependency,
            ) as formal_dependency_check:
                store.verify_integrity()
            self.assertGreater(formal_dependency_check.call_count, 0)

            with sqlite3.connect(store.paths.database) as connection:
                connection.execute(
                    "UPDATE raw_capture SET assignment_id = ? WHERE capture_id = ?",
                    ("session.formal.corrupt", capture.capture_id),
                )
            with self.assertRaises(WorkspaceIntegrityError):
                read_formal_session_revision(
                    store,
                    mission_id=MISSION_ID,
                    session_id=session_id,
                )
            with self.assertRaises(WorkspaceIntegrityError):
                read_formal_session_revision(
                    store,
                    mission_id=MISSION_ID,
                    session_id=session_id,
                    revision=2,
                )
            session_object_id = TypedWorkspaceId(IdentityKind.SESSION, session_id)
            with self.assertRaises(WorkspaceIntegrityError):
                store.get_head(session_object_id)
            with self.assertRaises(WorkspaceIntegrityError):
                store.get_revision(RevisionRef(session_object_id, 2))
            with self.assertRaises(WorkspaceIntegrityError):
                store.list_mission_formal_session_heads(MISSION_ID)
            with store.direct_recovery_read_scope():
                with self.assertRaises(WorkspaceIntegrityError):
                    store.read_root_owner_at_cut(
                        kind="session",
                        identity=session_id,
                        cut_project_commit=terminal_cut,
                    )
            with self.assertRaises(WorkspaceIntegrityError):
                store.commit_formal_session_revision(
                    executive_epoch_id=epoch_id,
                    mission_id=MISSION_ID,
                    session_id=session_id,
                    payload=terminal_payload,
                    expected_head_revision=1,
                    expected_head_payload_digest=prepared.record.payload_sha256,
                    dependency_heads=terminal_dependencies,
                    lease=lease,
                    command_id=terminal_command_id,
                    actor="formal-session-test",
                    expected_canonical_authority_digest=authority_digest,
                )
            with self.assertRaises(WorkspaceIntegrityError):
                store.verify_integrity()


if __name__ == "__main__":
    unittest.main()
