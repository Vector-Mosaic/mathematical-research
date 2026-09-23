from __future__ import annotations

import hashlib
import sys
import unittest
from pathlib import Path
from typing import Any, Mapping


PACKAGE_ROOT = Path(__file__).resolve().parents[1]
if str(PACKAGE_ROOT) not in sys.path:
    sys.path.insert(0, str(PACKAGE_ROOT))

from research_core.json_support import canonical_json_bytes  # noqa: E402
from research_core.mission_executive import (  # noqa: E402
    DirectExecutiveEpochAuthority,
    OwnerRevisionRef,
    prepare_direct_executive_epoch_authorized_event,
    prepare_direct_executive_epoch_bound_event,
    reissue_direct_executive_epoch_authority,
)
from research_core.mission_frontier import (  # noqa: E402
    FRONTIER_SCHEMA_VERSION,
    StrategyRevision,
    commit_branch_revision,
    commit_strategy_revision,
    derive_opportunity_portfolio,
    list_mission_branch_heads,
    prepare_branch_revision,
    prepare_strategy_revision,
    read_branch_revision,
    read_mission_strategy_head,
    read_strategy_revision,
)
from research_core.research_model import deep_thaw  # noqa: E402


def _digest(payload: Mapping[str, Any]) -> str:
    return hashlib.sha256(canonical_json_bytes(payload)).hexdigest()


def _authority(
    *,
    epoch_id: str = "epoch.frontier.1",
    mission_id: str = "mission.rh",
    mission_revision: int = 1,
    mission_digest: str = "1" * 64,
    project_id: str = "project.rh",
    root_identity: str = "root.rh",
    canonical_authority_digest: str = "f" * 64,
) -> DirectExecutiveEpochAuthority:
    authorized = prepare_direct_executive_epoch_authorized_event(
        executive_epoch_id=epoch_id,
        mission_root=OwnerRevisionRef(
            "mission", mission_id, mission_revision, mission_digest
        ),
        predecessor_checkpoint=None,
    )
    bound = prepare_direct_executive_epoch_bound_event(
        executive_epoch_id=epoch_id,
        mission_id=mission_id,
        goal_thread_id=f"thread:{epoch_id}",
        workspace_root="C:/work/rh",
    )

    def readback(event, ordinal: int, predecessor) -> Mapping[str, Any]:
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
            "created_at": f"2026-08-23T12:00:0{ordinal}Z",
            "row_digest": str(ordinal) * 64,
        }

    return reissue_direct_executive_epoch_authority(
        event_readbacks=(
            readback(authorized, 1, None),
            readback(bound, 2, authorized),
        ),
        project_id=project_id,
        root_identity=root_identity,
        canonical_authority_digest=canonical_authority_digest,
    )


def _ref(
    kind: str, identity: str, revision: int = 1, digest: str = "a" * 64
) -> Mapping[str, Any]:
    return {
        "kind": kind,
        "identity": identity,
        "revision": revision,
        "payload_sha256": digest,
    }


def _neutral_strategy_payload(
    *, mission_id: str = "mission.rh", strategy_id: str = "strategy.rh"
) -> Mapping[str, Any]:
    return {
        "schema_version": FRONTIER_SCHEMA_VERSION,
        "kind": "mission_strategy",
        "project_id": "project.rh",
        "mission_id": mission_id,
        "strategy_id": strategy_id,
        "mission_continuation": "continue",
        "integrated_comparison": (
            "No mathematical bet is selected; compare preserved owners before choosing."
        ),
        "causal_inputs": [],
        "serious_opportunities": [],
        "selected_bets": [],
        "attention_actions": [],
        "context_treatment": [],
        "creativity_treatment": [],
        "reconsideration_conditions": [],
        "reversal_conditions": [],
        "revival_conditions": [],
        "owner_refs": [],
    }


class FakeFrontierStore:
    def __init__(self) -> None:
        self.branches: dict[tuple[str, str], list[Mapping[str, Any]]] = {}
        self.strategies: dict[tuple[str, str], list[Mapping[str, Any]]] = {}
        self.last_branch_dependencies: Mapping[str, tuple[int, str]] | None = None
        self.last_strategy_dependencies: Mapping[str, tuple[int, str]] | None = None

    @staticmethod
    def _stored(
        *,
        identity_key: str,
        identity: str,
        mission_id: str,
        payload: Mapping[str, Any],
        revision: int,
        actor: str,
    ) -> Mapping[str, Any]:
        return {
            identity_key: identity,
            "mission_id": mission_id,
            "revision": revision,
            "payload": deep_thaw(payload),
            "payload_digest": _digest(payload),
            "predecessor_revision": None if revision == 1 else revision - 1,
            "created_actor": actor,
            "created_at": f"revision-{revision}",
        }

    @staticmethod
    def _commit(
        *,
        history: list[Mapping[str, Any]],
        identity_key: str,
        identity: str,
        mission_id: str,
        payload: Mapping[str, Any],
        expected_head_revision: int | None,
        expected_head_payload_digest: str | None,
        actor: str,
    ) -> Mapping[str, Any]:
        current = None if not history else history[-1]
        if current is None:
            if (
                expected_head_revision is not None
                or expected_head_payload_digest is not None
            ):
                raise ValueError("stale target")
            revision = 1
        else:
            if (
                current["revision"] != expected_head_revision
                or current["payload_digest"] != expected_head_payload_digest
            ):
                raise ValueError("stale target")
            if current["payload_digest"] == _digest(payload):
                return {"status": "no_op", "revision": current["revision"]}
            revision = int(current["revision"]) + 1
        history.append(
            FakeFrontierStore._stored(
                identity_key=identity_key,
                identity=identity,
                mission_id=mission_id,
                payload=payload,
                revision=revision,
                actor=actor,
            )
        )
        return {"status": "committed", "revision": revision}

    def seed_strategy(self, payload: Mapping[str, Any]) -> None:
        record = StrategyRevision(document=payload, payload_sha256=_digest(payload))
        mission_id = str(record.document["mission_id"])
        strategy_id = str(record.document["strategy_id"])
        self.strategies[(mission_id, strategy_id)] = [
            self._stored(
                identity_key="strategy_id",
                identity=strategy_id,
                mission_id=mission_id,
                payload=record.document,
                revision=1,
                actor="mission-genesis",
            )
        ]

    def commit_branch_revision(
        self,
        *,
        executive_epoch_id: str,
        mission_id: str,
        branch_id: str,
        payload: Mapping[str, Any],
        expected_head_revision: int | None,
        expected_head_payload_digest: str | None,
        dependency_heads: Mapping[str, tuple[int, str]] | None,
        lease: Any,
        command_id: str,
        actor: str,
        expected_canonical_authority_digest: str,
    ) -> Mapping[str, Any]:
        del lease, command_id, expected_canonical_authority_digest
        if executive_epoch_id != "epoch.frontier.1":
            raise ValueError("wrong Executive Epoch")
        self.last_branch_dependencies = dependency_heads
        history = self.branches.setdefault((mission_id, branch_id), [])
        return self._commit(
            history=history,
            identity_key="branch_id",
            identity=branch_id,
            mission_id=mission_id,
            payload=payload,
            expected_head_revision=expected_head_revision,
            expected_head_payload_digest=expected_head_payload_digest,
            actor=actor,
        )

    def read_branch_revision(
        self, *, mission_id: str, branch_id: str, revision: int | None = None
    ) -> Mapping[str, Any]:
        history = self.branches[(mission_id, branch_id)]
        return history[-1] if revision is None else history[revision - 1]

    def list_mission_branch_heads(
        self, mission_id: str
    ) -> tuple[Mapping[str, Any], ...]:
        return tuple(
            history[-1]
            for (stored_mission_id, _), history in self.branches.items()
            if stored_mission_id == mission_id
        )

    def commit_strategy_revision(
        self,
        *,
        executive_epoch_id: str,
        mission_id: str,
        strategy_id: str,
        payload: Mapping[str, Any],
        expected_head_revision: int | None,
        expected_head_payload_digest: str | None,
        dependency_heads: Mapping[str, tuple[int, str]] | None,
        lease: Any,
        command_id: str,
        actor: str,
        expected_canonical_authority_digest: str,
    ) -> Mapping[str, Any]:
        del lease, command_id, expected_canonical_authority_digest
        if executive_epoch_id != "epoch.frontier.1":
            raise ValueError("wrong Executive Epoch")
        self.last_strategy_dependencies = dependency_heads
        history = self.strategies[(mission_id, strategy_id)]
        return self._commit(
            history=history,
            identity_key="strategy_id",
            identity=strategy_id,
            mission_id=mission_id,
            payload=payload,
            expected_head_revision=expected_head_revision,
            expected_head_payload_digest=expected_head_payload_digest,
            actor=actor,
        )

    def read_strategy_revision(
        self, *, mission_id: str, strategy_id: str, revision: int | None = None
    ) -> Mapping[str, Any]:
        history = self.strategies[(mission_id, strategy_id)]
        return history[-1] if revision is None else history[revision - 1]

    def read_mission_strategy_head(self, mission_id: str) -> Mapping[str, Any]:
        matches = [
            history[-1]
            for (stored_mission_id, _), history in self.strategies.items()
            if stored_mission_id == mission_id
        ]
        if len(matches) != 1:
            raise ValueError("Mission must have exactly one Strategy head")
        return matches[0]


class MissionFrontierTests(unittest.TestCase):
    def setUp(self) -> None:
        self.store = FakeFrontierStore()
        self.store.seed_strategy(_neutral_strategy_payload())
        self.authority = _authority()

    def _branch(
        self,
        *,
        branch_id: str = "branch.theta",
        question: str = "Can theta positivity be reduced to a local kernel?",
        leverage: str = "theta-local-kernel",
        target_hook: str = "Produce a sign-preserving kernel identity.",
        genealogy: tuple[Mapping[str, Any], ...] = (),
        revival: bool = True,
    ):
        evidence_ref = _ref("evidence", f"evidence.{branch_id}")
        prepared = prepare_branch_revision(
            self.store,
            project_id="project.rh",
            mission_id="mission.rh",
            branch_id=branch_id,
            question=question,
            leverage_fingerprint=leverage,
            target_hook=target_hook,
            genealogy=genealogy,
            scoped_failures=(
                {
                    "scope": "constant-sign local kernels",
                    "finding": "the first obstruction changes sign",
                    "owner_refs": (evidence_ref,),
                },
            ),
            non_exclusions=(
                {
                    "scope": "signed nonlocal kernels",
                    "statement": "the local obstruction does not exclude them",
                    "owner_refs": (evidence_ref,),
                },
            ),
            retained_residue=(
                {
                    "residue": "an exact two-term identity survives",
                    "owner_refs": (evidence_ref,),
                },
            ),
            composition_interfaces=(
                {"interface": "compose with heat flow", "owner_refs": ()},
            ),
            recombination_interfaces=(
                {"interface": "signed recombination", "owner_refs": ()},
            ),
            revival_conditions=(
                (
                    {
                        "condition": "a source supplies a nonlocal sign identity",
                        "owner_refs": (),
                    },
                )
                if revival
                else ()
            ),
            nonclaims=("No claim of RH resolution.",),
            owner_refs=(),
        )
        commit_branch_revision(
            self.store,
            authority=self.authority,
            prepared=prepared,
            lease=object(),
            actor="executive",
        )
        return read_branch_revision(
            self.store,
            mission_id="mission.rh",
            branch_id=branch_id,
        )

    def test_branch_is_canonical_target_local_and_semantic_replay_is_noop(
        self,
    ) -> None:
        first = self._branch()
        self.assertEqual(first.revision, 1)
        self.assertEqual(
            self.store.last_branch_dependencies,
            {"evidence:evidence.branch.theta": (1, "a" * 64)},
        )
        document = first.record.document
        self.assertNotIn("attention", document)
        self.assertNotIn("lifecycle", document)
        self.assertNotIn("worker", document)
        self.assertNotIn("result", document)

        prepared = prepare_branch_revision(
            self.store,
            project_id="project.rh",
            mission_id="mission.rh",
            branch_id="branch.theta",
            question=document["question"],
            leverage_fingerprint=document["leverage_fingerprint"],
            target_hook=document["target_hook"],
            genealogy=document["genealogy"],
            scoped_failures=tuple(reversed(document["scoped_failures"])),
            non_exclusions=document["non_exclusions"],
            retained_residue=document["retained_residue"],
            composition_interfaces=document["composition_interfaces"],
            recombination_interfaces=document["recombination_interfaces"],
            revival_conditions=document["revival_conditions"],
            nonclaims=tuple(reversed(document["nonclaims"])),
            owner_refs=document["owner_refs"],
        )
        outcome = commit_branch_revision(
            self.store,
            authority=self.authority,
            prepared=prepared,
            lease=object(),
            actor="executive",
        )
        self.assertEqual(outcome, {"status": "no_op", "revision": 1})
        self.assertEqual(len(self.store.branches[("mission.rh", "branch.theta")]), 1)
        self.assertEqual(
            [item.record.document["branch_id"] for item in list_mission_branch_heads(
                self.store, mission_id="mission.rh"
            )],
            ["branch.theta"],
        )

    def test_material_identity_change_requires_an_explicit_linked_successor(
        self,
    ) -> None:
        predecessor = self._branch()
        with self.assertRaisesRegex(ValueError, "linked successor"):
            prepare_branch_revision(
                self.store,
                project_id="project.rh",
                mission_id="mission.rh",
                branch_id="branch.theta",
                question="A materially different question",
                leverage_fingerprint="theta-local-kernel",
                target_hook="Produce a sign-preserving kernel identity.",
            )

        successor = self._branch(
            branch_id="branch.theta.nonlocal",
            question="Can a signed nonlocal kernel preserve the target sign?",
            leverage="theta-nonlocal-kernel",
            target_hook="Construct a nonlocal sign identity.",
            genealogy=(
                {
                    "relationship": "successor_of",
                    "branch": predecessor.to_reference(),
                },
            ),
        )
        self.assertEqual(
            successor.record.document["genealogy"][0]["branch"],
            predecessor.to_reference(),
        )
        with self.assertRaisesRegex(ValueError, "at least two"):
            prepare_branch_revision(
                self.store,
                project_id="project.rh",
                mission_id="mission.rh",
                branch_id="branch.bad-merge",
                question="Can these routes be merged?",
                leverage_fingerprint="merge",
                target_hook="Establish semantic identity.",
                genealogy=(
                    {
                        "relationship": "merge_of",
                        "branch": predecessor.to_reference(),
                    },
                ),
            )

    def test_strategy_revises_only_the_existing_mission_lineage(self) -> None:
        branch = self._branch()
        evidence_ref = _ref("evidence", "evidence.theta")
        context_ref = _ref("context", "context.theta", digest="b" * 64)
        prepared = prepare_strategy_revision(
            self.store,
            project_id="project.rh",
            mission_id="mission.rh",
            strategy_id="strategy.rh",
            mission_continuation="continue",
            integrated_comparison=(
                "The nonlocal route survives the local obstruction and displaces "
                "another local construction pass."
            ),
            causal_inputs=(
                {
                    "source_ref": evidence_ref,
                    "decision_consequence": "rules out the constant-sign local bet",
                },
            ),
            serious_opportunities=(
                {
                    "relationship": "complement",
                    "opportunity_ref": branch.to_reference(),
                    "qualitative_opportunity_cost": "delays source transfer",
                },
            ),
            selected_bets=(
                {
                    "bet": "test the surviving nonlocal interface",
                    "discriminator": "derive or falsify one sign identity",
                    "owner_refs": (branch.to_reference(),),
                },
            ),
            attention_actions=(
                {
                    "branch_ref": branch.to_reference(),
                    "attention": "active",
                    "consequence": "construct the exact discriminator",
                },
            ),
            context_treatment=(
                {
                    "context_ref": context_ref,
                    "treatment": "retain the exact normalization",
                },
            ),
            creativity_treatment=(
                {
                    "treatment": "transfer a nonlocal representation",
                    "decision_consequence": "tests a distinct mechanism",
                    "owner_refs": (),
                },
            ),
            reconsideration_conditions=(
                {"condition": "the sign identity is resolved", "owner_refs": ()},
            ),
            reversal_conditions=(
                {"condition": "the nonlocal obstruction is exact", "owner_refs": ()},
            ),
            revival_conditions=(
                {
                    "condition": "a source supplies the missing transform",
                    "owner_refs": (),
                },
            ),
        )
        outcome = commit_strategy_revision(
            self.store,
            authority=self.authority,
            prepared=prepared,
            lease=object(),
            actor="executive",
        )
        self.assertEqual(outcome, {"status": "committed", "revision": 2})
        current = read_mission_strategy_head(self.store, mission_id="mission.rh")
        historical = read_strategy_revision(
            self.store,
            mission_id="mission.rh",
            strategy_id="strategy.rh",
            revision=1,
        )
        self.assertEqual(current.revision, 2)
        self.assertEqual(historical.revision, 1)
        self.assertEqual(
            set(self.store.last_strategy_dependencies or {}),
            {
                "branch:branch.theta",
                "context:context.theta",
                "evidence:evidence.theta",
            },
        )
        self.assertEqual(
            set(current.record.document["attention_actions"][0]),
            {"branch_ref", "attention", "consequence"},
        )
        with self.assertRaisesRegex(ValueError, "cannot mint"):
            prepare_strategy_revision(
                self.store,
                project_id="project.rh",
                mission_id="mission.rh",
                strategy_id="strategy.another",
                mission_continuation="continue",
                integrated_comparison="Compare the current serious opportunities.",
            )
        with self.assertRaisesRegex(ValueError, "unsupported"):
            prepare_strategy_revision(
                self.store,
                project_id="project.rh",
                mission_id="mission.rh",
                strategy_id="strategy.rh",
                mission_continuation="continue",
                integrated_comparison="Compare the current serious opportunities.",
                attention_actions=(
                    {
                        "branch_ref": branch.to_reference(),
                        "attention": "paused",
                        "consequence": "not an accepted attention judgment",
                    },
                ),
            )
        with self.assertRaisesRegex(ValueError, "non-empty"):
            prepare_strategy_revision(
                self.store,
                project_id="project.rh",
                mission_id="mission.rh",
                strategy_id="strategy.rh",
                mission_continuation="continue",
                integrated_comparison=" ",
            )

        with self.assertRaisesRegex(ValueError, "historical readback"):
            prepare_strategy_revision(
                self.store,
                project_id="project.rh",
                mission_id="mission.rh",
                strategy_id="strategy.rh",
                mission_continuation="pause",
                integrated_comparison="A researcher cannot stop the Mission runtime.",
            )

    def test_historical_strategy_pause_and_closeout_remain_readable_only(self) -> None:
        for historical_continuation in ("pause", "closeout"):
            legacy_store = FakeFrontierStore()
            legacy_payload = deep_thaw(_neutral_strategy_payload())
            legacy_payload["mission_continuation"] = historical_continuation
            legacy_store.seed_strategy(legacy_payload)

            persisted = read_mission_strategy_head(
                legacy_store, mission_id="mission.rh"
            )
            self.assertEqual(
                persisted.record.document["mission_continuation"],
                historical_continuation,
            )
            with self.assertRaisesRegex(ValueError, "only continue"):
                prepare_strategy_revision(
                    legacy_store,
                    project_id="project.rh",
                    mission_id="mission.rh",
                    strategy_id="strategy.rh",
                    mission_continuation=historical_continuation,
                    integrated_comparison=(
                        "Historical terminal meaning remains readable without "
                        "conferring new ordinary write authority."
                    ),
                )

        with self.assertRaisesRegex(ValueError, "only continue"):
            prepare_strategy_revision(
                self.store,
                project_id="project.rh",
                mission_id="mission.rh",
                strategy_id="strategy.rh",
                mission_continuation="closeout",
                integrated_comparison="Ordinary authoring cannot close the Mission.",
            )

    def test_strategy_semantic_reordering_is_store_noop(self) -> None:
        branch_a = self._branch(branch_id="branch.a", revival=False)
        branch_b = self._branch(branch_id="branch.b", revival=False)
        formal_context = _ref(
            "context", "context.formal-a", revision=2, digest="c" * 64
        )
        kwargs = {
            "project_id": "project.rh",
            "mission_id": "mission.rh",
            "strategy_id": "strategy.rh",
            "mission_continuation": "continue",
            "integrated_comparison": (
                "Branch a has the sharper discriminator; branch b remains complementary."
            ),
            "selected_bets": (
                {
                    "bet": "bet b",
                    "discriminator": "test b",
                    "owner_refs": (branch_b.to_reference(),),
                },
                {
                    "bet": "bet a",
                    "discriminator": "test a",
                    "owner_refs": (branch_a.to_reference(),),
                    "formal_request": {
                        "purpose": "targeted_verification",
                        "context_ref": formal_context,
                    },
                },
            ),
            "attention_actions": (
                {
                    "branch_ref": branch_b.to_reference(),
                    "attention": "available",
                    "consequence": "retain as a serious alternative",
                },
                {
                    "branch_ref": branch_a.to_reference(),
                    "attention": "active",
                    "consequence": "run its discriminator",
                },
            ),
        }
        first = prepare_strategy_revision(self.store, **kwargs)
        commit_strategy_revision(
            self.store,
            authority=self.authority,
            prepared=first,
            lease=object(),
            actor="executive",
        )
        self.assertEqual(
            (self.store.last_strategy_dependencies or {})[
                "context:context.formal-a"
            ],
            (2, "c" * 64),
        )
        formal_bet = next(
            item
            for item in first.record.document["selected_bets"]
            if "formal_request" in item
        )
        self.assertEqual(
            formal_bet["formal_request"],
            {
                "purpose": "targeted_verification",
                "context_ref": formal_context,
            },
        )
        ordinary_bet = next(
            item
            for item in first.record.document["selected_bets"]
            if item["bet"] == "bet b"
        )
        self.assertNotIn("formal_request", ordinary_bet)
        reordered = dict(kwargs)
        reordered["selected_bets"] = tuple(reversed(kwargs["selected_bets"]))
        reordered["attention_actions"] = tuple(
            reversed(kwargs["attention_actions"])
        )
        second = prepare_strategy_revision(self.store, **reordered)
        self.assertEqual(first.record.payload_sha256, second.record.payload_sha256)
        outcome = commit_strategy_revision(
            self.store,
            authority=self.authority,
            prepared=second,
            lease=object(),
            actor="executive",
        )
        self.assertEqual(outcome, {"status": "no_op", "revision": 2})

    def test_formal_request_rejects_wrong_purpose_kind_and_extra_fields(self) -> None:
        base = {
            "project_id": "project.rh",
            "mission_id": "mission.rh",
            "strategy_id": "strategy.rh",
            "mission_continuation": "continue",
            "integrated_comparison": "One exact discriminator warrants a formal check.",
        }
        invalid_requests = (
            {
                "purpose": "open_ended_research",
                "context_ref": _ref("context", "context.formal"),
            },
            {
                "purpose": "targeted_falsification",
                "context_ref": _ref("candidate", "candidate.not-context"),
            },
            {
                "purpose": "targeted_verification",
                "context_ref": _ref("context", "context.formal"),
                "max_attempts": 1,
            },
        )
        for formal_request in invalid_requests:
            with self.subTest(formal_request=formal_request):
                with self.assertRaisesRegex(ValueError, "unsupported|context|closed"):
                    prepare_strategy_revision(
                        self.store,
                        **base,
                        selected_bets=(
                            {
                                "bet": "test one exact discriminator",
                                "discriminator": "derive or falsify the identity",
                                "owner_refs": (),
                                "formal_request": formal_request,
                            },
                        ),
                    )

    def test_opportunity_portfolio_is_pure_explicit_and_order_neutral(self) -> None:
        branch_a = self._branch(branch_id="branch.a")
        branch_b = self._branch(branch_id="branch.b", revival=False)
        prepared = prepare_strategy_revision(
            self.store,
            project_id="project.rh",
            mission_id="mission.rh",
            strategy_id="strategy.rh",
            mission_continuation="continue",
            integrated_comparison=(
                "Branch a has the immediate sign test while branch b remains available."
            ),
            selected_bets=(
                {
                    "bet": "construct branch a discriminator",
                    "discriminator": "one exact sign test",
                    "owner_refs": (branch_a.to_reference(),),
                },
            ),
            attention_actions=(
                {
                    "branch_ref": branch_a.to_reference(),
                    "attention": "active",
                    "consequence": "perform the sign test",
                },
                {
                    "branch_ref": branch_b.to_reference(),
                    "attention": "available",
                    "consequence": "preserve the complementary route",
                },
            ),
        )
        commit_strategy_revision(
            self.store,
            authority=self.authority,
            prepared=prepared,
            lease=object(),
            actor="executive",
        )
        strategy = read_mission_strategy_head(self.store, mission_id="mission.rh")
        synthesis_hooks = (
            {
                "hook": "combine the surviving transform with branch b",
                "discriminator": "derive one shared boundary identity",
                "owner_refs": (branch_b.to_reference(), branch_a.to_reference()),
            },
        )
        historical_hooks = (
            {
                "hook": "recover the exact prior heat-kernel obstruction",
                "reversal_condition": "the old normalization was inapplicable",
                "owner_refs": (_ref("evidence", "evidence.historical"),),
            },
        )
        deltas = (_ref("candidate", "candidate.new"), branch_b.to_reference())
        first = derive_opportunity_portfolio(
            strategy=strategy,
            branch_heads=(branch_b, branch_a),
            synthesis_hooks=synthesis_hooks,
            historical_hooks=historical_hooks,
            unincorporated_owner_refs=deltas,
        )
        second = derive_opportunity_portfolio(
            strategy=strategy,
            branch_heads=(branch_a, branch_b),
            synthesis_hooks=tuple(reversed(synthesis_hooks)),
            historical_hooks=tuple(reversed(historical_hooks)),
            unincorporated_owner_refs=tuple(reversed(deltas)),
        )
        self.assertEqual(first, second)
        self.assertEqual(len(first["branch_revival_hooks"]), 1)
        self.assertEqual(len(first["synthesis_hooks"]), 1)
        self.assertEqual(len(first["historical_hooks"]), 1)
        self.assertEqual(
            first["integrated_comparison"],
            strategy.record.document["integrated_comparison"],
        )
        self.assertNotIn("portfolio_id", first)
        self.assertFalse(
            {
                "score",
                "rank",
                "probability",
                "allocation",
                "cap",
                "cooldown",
                "dispatch",
                "priority",
            }
            & set(first)
        )



if __name__ == "__main__":
    unittest.main()
