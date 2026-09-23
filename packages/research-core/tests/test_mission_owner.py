from __future__ import annotations

import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock


PACKAGE_ROOT = Path(__file__).resolve().parents[1]
if str(PACKAGE_ROOT) not in sys.path:
    sys.path.insert(0, str(PACKAGE_ROOT))

from research_core.mission_owner import (  # noqa: E402
    MissionFenceReason,
    MissionOwnerError,
    direct_mission_astra_successor,
    direct_mission_fence_successor,
    direct_mission_reauthorization_successor,
    direct_mission_scientific_context_successor,
    successor_mission_contract,
)
from research_core.research_model import deep_thaw  # noqa: E402


class MissionOwnerTests(unittest.TestCase):
    def test_contract_has_fixed_model_and_no_repository_research_ceiling(self) -> None:
        contract = deep_thaw(successor_mission_contract())

        self.assertEqual(contract["execution_policy"]["model"], "gpt-6-astra")
        self.assertEqual(contract["execution_policy"]["reasoning_effort"], "ultra")
        self.assertFalse(contract["execution_policy"]["model_fallback"])
        self.assertEqual(
            contract["execution_policy"]["baseline_capabilities"],
            ["rh_mission", "shell", "web_search", "native_delegation"],
        )
        serialized = repr(contract).lower()
        for forbidden in (
            "token_budget",
            "timeout_seconds",
            "max_attempts",
            "max_concurrent",
            "resource_ceiling",
        ):
            self.assertNotIn(forbidden, serialized)

    def test_selected_capabilities_are_exact_and_secretless(self) -> None:
        contract = successor_mission_contract(
            selected_capabilities={
                "local_roots": [
                    {
                        "kind": "skill",
                        "id": "proof-library",
                        "release_relative_path": "skills/proof-library",
                    }
                ],
                "mcp_servers": [
                    {"server_id": "arxiv", "enabled_tools": ["search", "read"]}
                ],
                "apps": [{"app_id": "wolfram", "enabled_tools": ["evaluate"]}],
                "browser": "isolated_ephemeral_unauthenticated",
            }
        )

        self.assertEqual(successor_mission_contract(contract), contract)
        self.assertTrue(contract["execution_policy"]["network"]["secretless"])
        self.assertFalse(
            contract["execution_policy"]["network"]["credential_inheritance"]
        )
        with self.assertRaises(MissionOwnerError):
            successor_mission_contract(
                selected_capabilities={
                    "local_roots": [],
                    "mcp_servers": [],
                    "apps": [],
                    "browser": "signed_in_personal_profile",
                }
            )

    def test_historical_contract_is_validated_without_rewriting_it(self) -> None:
        historical = deep_thaw(successor_mission_contract())
        historical["execution_policy"]["model"] = "gpt-5.6-sol"
        self.assertEqual(deep_thaw(successor_mission_contract(historical)), historical)
        for field, value in (
            ("model", "latest"),
            ("reasoning_effort", "high"),
            ("model_fallback", True),
            ("automatic_installation", True),
        ):
            with self.subTest(field=field):
                invalid = deep_thaw(historical)
                invalid["execution_policy"][field] = value
                with self.assertRaises(MissionOwnerError):
                    successor_mission_contract(invalid)

    def test_astra_successor_changes_only_model_and_control_revision(self) -> None:
        mission = {
            "mission_id": "mission.rh",
            "revision": 1,
            "project_id": "project.rh",
            "coordination_epoch": "epoch.rh",
            "authorization_id": "authorization.rh",
            "control_revision": 3,
            "lifecycle": "active",
            "fence_reason": None,
            "fenced_at": None,
            "autonomous": True,
            "effective": True,
            "strategy_ids": ["strategy.rh"],
            **deep_thaw(successor_mission_contract()),
        }
        mission["execution_policy"]["model"] = "gpt-5.6-sol"
        migrated = deep_thaw(direct_mission_astra_successor(mission))
        expected = deep_thaw(mission)
        expected["execution_policy"]["model"] = "gpt-6-astra"
        expected["control_revision"] += 1
        expected["scientific_context_id"] = None
        self.assertEqual(migrated, expected)
        self.assertEqual(mission["execution_policy"]["model"], "gpt-5.6-sol")
        with self.assertRaises(MissionOwnerError):
            direct_mission_astra_successor(migrated)
        with self.assertRaises(MissionOwnerError):
            direct_mission_astra_successor(
                direct_mission_fence_successor(mission, fenced_at="2026-09-04T12:00:00Z")
            )

    def test_fence_is_one_field_only_direct_successor(self) -> None:
        mission = {
            "mission_id": "mission.rh",
            "revision": 1,
            "project_id": "project.rh",
            "coordination_epoch": "epoch.rh",
            "authorization_id": "authorization.rh",
            "control_revision": 3,
            "lifecycle": "active",
            "fence_reason": None,
            "fenced_at": None,
            "autonomous": True,
            "effective": True,
            "strategy_ids": ["strategy.rh"],
            **deep_thaw(successor_mission_contract()),
        }

        fenced = deep_thaw(
            direct_mission_fence_successor(
                mission, fenced_at="2026-08-24T12:00:00+00:00"
            )
        )

        self.assertEqual(fenced["fence_reason"], MissionFenceReason.REVOKED.value)
        self.assertEqual(fenced["lifecycle"], "held")
        self.assertFalse(fenced["effective"])
        self.assertEqual(fenced["control_revision"], 4)
        self.assertEqual(fenced["purpose"], mission["purpose"])
        self.assertEqual(fenced["execution_policy"], mission["execution_policy"])
        self.assertIsNone(fenced["scientific_context_id"])
        self.assertNotIn("scientific_context_id", mission)

        historical = {**mission, "unresolved_decision_bundle_ceiling": 16}
        historical["execution_policy"] = {
            **historical["execution_policy"], "model": "gpt-5.6-sol"
        }
        historical_fence = direct_mission_fence_successor(
            historical, fenced_at="2026-08-24T12:00:00+00:00"
        )
        reauthorized = direct_mission_reauthorization_successor(historical_fence)
        migrated = direct_mission_astra_successor(reauthorized)
        for successor in (historical_fence, reauthorized, migrated):
            self.assertEqual(successor["unresolved_decision_bundle_ceiling"], 16)
            self.assertNotIn("unresolved_decision_bundle_ceiling", successor["execution_policy"])
            self.assertNotIn("unresolved_decision_bundle_ceiling", successor["purpose"])
        self.assertEqual(historical["execution_policy"]["model"], "gpt-5.6-sol")

        for derive, source in (
            (lambda value: direct_mission_fence_successor(value, fenced_at="2026-08-24T12:00:00Z"), historical),
            (direct_mission_reauthorization_successor, historical_fence),
            (direct_mission_astra_successor, reauthorized),
        ):
            with self.subTest(derive=derive.__name__), self.assertRaises(MissionOwnerError):
                derive({**source, "unrecognized_history": 16})


class ScientificContextMissionBindingTests(unittest.TestCase):
    @staticmethod
    def _mission():
        return {
            "mission_id": "mission.rh",
            "revision": 2,
            "project_id": "project.rh",
            "coordination_epoch": "epoch.rh",
            "authorization_id": "authorization.rh",
            "control_revision": 3,
            "lifecycle": "active",
            "fence_reason": None,
            "fenced_at": None,
            "autonomous": True,
            "effective": True,
            "strategy_ids": ["strategy.rh"],
            **deep_thaw(successor_mission_contract()),
        }

    def test_binding_changes_only_selected_relationship_and_control_revision(self):
        historical = self._mission()
        before = deep_thaw(historical)
        bound = deep_thaw(direct_mission_scientific_context_successor(
            historical, context_id="science.rh"
        ))
        expected = {**before, "scientific_context_id": "science.rh", "control_revision": 4}
        self.assertEqual(bound, expected)
        self.assertEqual(historical, before)
        self.assertNotIn("scientific_context_id", historical)

        rebound = deep_thaw(direct_mission_scientific_context_successor(
            bound, context_id="science.alternative"
        ))
        self.assertEqual(rebound, {
            **bound, "scientific_context_id": "science.alternative", "control_revision": 5
        })
        self.assertEqual(bound["scientific_context_id"], "science.rh")

    def test_all_lifecycle_successors_preserve_explicit_binding_and_null(self):
        for context_id in (None, "science.rh"):
            with self.subTest(context_id=context_id):
                mission = self._mission()
                mission["scientific_context_id"] = context_id
                mission["execution_policy"]["model"] = "gpt-5.6-sol"
                fenced = direct_mission_fence_successor(
                    mission, fenced_at="2026-09-14T12:00:00Z"
                )
                reauthorized = direct_mission_reauthorization_successor(fenced)
                migrated = direct_mission_astra_successor(reauthorized)
                for successor in (fenced, reauthorized, migrated):
                    self.assertIn("scientific_context_id", successor)
                    self.assertEqual(successor["scientific_context_id"], context_id)
                    self.assertEqual(successor["strategy_ids"], tuple(mission["strategy_ids"]))
                    self.assertEqual(deep_thaw(successor["purpose"]), mission["purpose"])

    def test_old_effect_reconstruction_is_explicit_and_does_not_rewrite_history(self):
        mission = self._mission()
        mission["execution_policy"]["model"] = "gpt-5.6-sol"
        historical_fence = direct_mission_fence_successor(
            mission, fenced_at="2026-09-14T12:00:00Z", preserve_historical_shape=True
        )
        historical_reauthorization = direct_mission_reauthorization_successor(
            historical_fence, preserve_historical_shape=True
        )
        historical_migration = direct_mission_astra_successor(
            historical_reauthorization, preserve_historical_shape=True
        )
        for retained in (mission, historical_fence, historical_reauthorization, historical_migration):
            self.assertNotIn("scientific_context_id", retained)
        for new_revision in (
            direct_mission_fence_successor(mission, fenced_at="2026-09-14T12:00:00Z"),
            direct_mission_reauthorization_successor(historical_fence),
            direct_mission_astra_successor(historical_reauthorization),
        ):
            self.assertIn("scientific_context_id", new_revision)
            self.assertIsNone(new_revision["scientific_context_id"])

    def test_binding_rejects_reference_revision_path_title_and_nontext_targets(self):
        for invalid in (
            None, "", "context:science.rh", "science.rh@2", "Context title",
            "science/rh", r"C:\science\rh", {"context_id": "science.rh"}, 1,
        ):
            with self.subTest(invalid=invalid), self.assertRaises(MissionOwnerError):
                direct_mission_scientific_context_successor(self._mission(), context_id=invalid)

    def test_new_shape_is_closed_and_invalid_stored_binding_is_not_ignored(self):
        for invalid in ("context:science.rh", "science.rh@2", 1, {}):
            with self.subTest(invalid=invalid), self.assertRaises(MissionOwnerError):
                direct_mission_fence_successor(
                    {**self._mission(), "scientific_context_id": invalid},
                    fenced_at="2026-09-14T12:00:00Z",
                )
        with self.assertRaises(MissionOwnerError):
            direct_mission_scientific_context_successor(
                {**self._mission(), "scientific_context_id": None, "science_title": "latest"},
                context_id="science.rh",
            )

    def test_owner_interface_pins_preimages_and_returns_original_result_on_replay(self):
        from research_core.mission_interface import MissionInterface
        from research_core.workspace_schema import IdentityKind, RevisionRef, TypedWorkspaceId

        interface = object.__new__(MissionInterface)
        interface._mission_id = "mission.rh"
        target_ref = RevisionRef(TypedWorkspaceId(IdentityKind.MISSION, "mission.rh"), 3)
        store = mock.Mock(project_id="project.rh")
        store.bind_mission_scientific_context.return_value = SimpleNamespace(
            changed_heads=(target_ref,), replayed=False, project_commit=7
        )
        store.get_revision.return_value = SimpleNamespace(
            reference=target_ref, payload_digest="d" * 64,
            payload={"scientific_context_id": "science.rh"},
        )
        interface._store = store
        arguments = {
            "expected_mission_revision": 2,
            "expected_mission_payload_sha256": "a" * 64,
            "expected_canonical_authority_digest": "b" * 64,
            "context_id": "science.rh",
            "expected_context_revision": 4,
            "expected_context_payload_sha256": "c" * 64,
        }
        with mock.patch.object(interface, "_writer_lease", return_value="writer"):
            first = interface.bind_scientific_context_from_owner(**arguments)
            first_command = store.bind_mission_scientific_context.call_args.kwargs
            store.bind_mission_scientific_context.return_value.replayed = True
            replay = interface.bind_scientific_context_from_owner(**arguments)
            replay_command = store.bind_mission_scientific_context.call_args.kwargs
        self.assertEqual(first_command, replay_command)
        self.assertEqual(first_command["context_id"], "science.rh")
        self.assertEqual(first_command["expected_context_revision"], 4)
        self.assertEqual(first_command["expected_context_payload_digest"], "c" * 64)
        self.assertEqual(first_command["expected_head_revision"], 2)
        self.assertEqual(first_command["expected_head_payload_digest"], "a" * 64)
        self.assertEqual(first_command["expected_canonical_authority_digest"], "b" * 64)
        self.assertEqual(replay, {**first, "idempotent": True})
        self.assertEqual(replay["target_mission_ref"], {"revision": 3, "payload_sha256": "d" * 64})
        self.assertEqual(replay["scientific_context_ref"]["revision"], 4)
        store.get_revision.assert_called_with(target_ref)
        store.get_head.assert_not_called()


if __name__ == "__main__":
    unittest.main()
