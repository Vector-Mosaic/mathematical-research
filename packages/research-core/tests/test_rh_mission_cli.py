from __future__ import annotations

import importlib.util
import hashlib
import io
import json
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch


REPO_ROOT = Path(__file__).resolve().parents[3]
SCRIPT = REPO_ROOT / "scripts" / "rh_mission.py"
SPEC = importlib.util.spec_from_file_location("rh_mission_tool", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
TOOL = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = TOOL
SPEC.loader.exec_module(TOOL)


def _request(operation: str, payload: dict[str, object]) -> dict[str, object]:
    return {
        "schema_version": TOOL.MISSION_SEMANTIC_REQUEST_SCHEMA_VERSION,
        "operation": operation,
        "input": payload,
    }


def _strategy_request() -> dict[str, object]:
    return _request(
        "record_strategy",
        {
            "mission_continuation": "continue",
            "integrated_comparison": (
                "The sign-gap obstruction currently dominates the credible alternatives."
            ),
            "reconsideration_conditions": [
                {
                    "condition": (
                        "Reconsider when exact Evidence changes the obstruction."
                    )
                }
            ],
        },
    )


def _bridge_request(
    action: str,
    payload: dict[str, object],
    *,
    project_id: str = "project.rh",
    mission_id: str = "mission.1",
) -> dict[str, object]:
    return {
        "schema_version": TOOL.MISSION_HOST_BRIDGE_REQUEST_SCHEMA_VERSION,
        "action": action,
        "binding": {
            "project_id": project_id,
            "mission_id": mission_id,
        },
        "payload": payload,
    }


def _epoch_projection(
    *,
    root_thread_id: str | None = "thread.root",
    state: str = "open",
) -> tuple[dict[str, object], str]:
    epoch_id = "epoch.1"
    return (
        {
            "state": state,
            "entry": {
                "executive_epoch_id": epoch_id,
                "goal_thread_id": root_thread_id,
                "workspace_root": (
                    None
                    if root_thread_id is None
                    else "C:\\rh-mission\\workspace"
                ),
            },
        },
        epoch_id,
    )


def _orientation_result() -> dict[str, object]:
    """Closed adapter fixture; owner semantics are covered by owner integration."""
    from research_core.executive_orientation import semantic_owner_document
    from research_core.mission_operation_contract import validate_semantic_result
    seed = json.loads((REPO_ROOT / "contracts/rh_autonomous_mission_seed.v1.json").read_text(encoding="utf-8"))
    orientation = {
        "schema_version": "mathematical_research.executive_orientation.v3",
        "target": {"target": "riemann_hypothesis", "statement": "Every nontrivial zero of the Riemann zeta function has real part one half.", "canonical_status": "open"},
        "mission": {"handle": "mission:mission.1@1", **semantic_owner_document("mission", seed["mission"])},
        "current_strategy": {"handle": "strategy:strategy.1@1", **semantic_owner_document("strategy", seed["strategy"]), "formal_requests": []},
        "scientific_context": {
            "state": "unbound", "binding": None, "root_reference": None,
            "known_omissions": [], "restricted_uses": [], "restrictions": [], "independence_treatment": None,
            "purpose": None, "question": None, "treatments": [],
            "source_changes": [], "unavailable": [],
        },
        "continuity": {"checkpoint": None, "mission_relation_to_checkpoint": "no_checkpoint", "strategy_relation_to_checkpoint": "no_checkpoint",
            "mission_strategy_changes_since_checkpoint": {"state": "no_checkpoint", "counts_by_kind": {}, "retrieve_call": None},
            "checkpoint_attention": {"unresolved_pointer_count": 0, "retrieve_call": None}},
        "proof_attention": {"open_candidate_a1": [], "admitted_result": None}, "formal_attention": [],
        "retrieval": {"usage_call": {"operation": "usage", "input": {"for_operation": "retrieve"}},
            "available_modes": ["read", "search", "inventory", "checkpoint", "changes_since_checkpoint", "hooks", "captures", "proof_attention"], "recommended_calls": []},
    }
    result = {"schema_version": TOOL.MISSION_SEMANTIC_RESULT_SCHEMA_VERSION, "operation": "orient", "status": "completed", "result": {"orientation": orientation}, "error": None}
    validate_semantic_result("orient", result)
    return result


class RhMissionCliTests(unittest.TestCase):


    def _run(self, *args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, str(SCRIPT), *args],
            cwd=REPO_ROOT,
            text=True,
            capture_output=True,
            check=False,
        )

    def _run_bridge(
        self,
        request: dict[str, object],
        facade: object,
        *,
        project_id: str = "project.rh",
        mission_id: str = "mission.1",
    ) -> tuple[int, dict[str, object]]:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "bridge.json"
            response_path = Path(temporary) / "bridge-response.json"
            path.write_text(json.dumps(request), encoding="utf-8")
            with patch.object(TOOL.MissionInterface, "open", return_value=facade):
                stream = io.StringIO()
                with redirect_stdout(stream):
                    exit_code = TOOL.main(
                        [
                            "host-bridge",
                            "--workspace-root",
                            "C:\\rh-mission\\workspace",
                            "--project-id",
                            project_id,
                            "--mission-id",
                            mission_id,
                            "--request",
                            str(path.resolve()),
                            "--response",
                            str(response_path.resolve()),
                            "--format",
                            "json",
                        ]
                    )
            result = json.loads(response_path.read_text(encoding="utf-8"))
            self.assertEqual(stream.getvalue(), "")
        return exit_code, result

    def test_safe_handshake_exposes_only_canonical_semantic_operations(self) -> None:
        no_arg = self._run()
        self.assertEqual(no_arg.returncode, 0, no_arg.stderr)
        self.assertIn("stability: experimental", no_arg.stdout)

        usage = self._run("usage", "--format", "json")
        self.assertEqual(usage.returncode, 0, usage.stderr)
        payload = json.loads(usage.stdout)
        self.assertEqual(payload["schema_version"], "tool_usage.v1")
        self.assertEqual(payload["tool_id"], TOOL.TOOL_ID)
        self.assertEqual(payload["lifecycle"], "noncanonical_runtime")
        self.assertEqual(
            [item["name"] for item in payload["verbs"]],
            [
                "usage",
                "capabilities",
                "owner-genesis",
                "owner-migrate-model-policy",
                "owner-bind-scientific-context",
                "owner-revise-scientific-context",
                "owner-reauthorize-mission",
                "host-bridge",
                "orient",
                "retrieve",
                "record-context",
                "interpret-material",
                "record-candidate",
                "record-branch",
                "synthesize",
                "record-strategy",
                "checkpoint",
            ],
        )
        self.assertNotIn(
            "evidence-ingest-native",
            [item["name"] for item in payload["verbs"]],
        )
        self.assertFalse(payload["host_bridge"]["model_visible"])
        self.assertEqual(
            payload["host_bridge"]["actions"],
            {
                "authorize_executive_epoch": {
                    "owner_method": "authorize_executive_epoch_from_owner"
                },
                "validate_suspended_epoch_cut": {
                    "owner_method": "validate_suspended_epoch_cut_from_owner"
                },
                "bind_executive_epoch": {
                    "owner_method": "bind_executive_epoch_from_owner"
                },
                "execute_semantic_operation": {
                    "owner_method": "execute_semantic_operation"
                },
                "issue_research_read_grant": {
                    "owner_method": "issue_research_read_grant"
                },
                "execute_research_read": {
                    "owner_method": "execute_research_read"
                },
                "issue_historical_read_grant": {
                    "owner_method": "issue_historical_read_grant"
                },
                "execute_delegated_read": {
                    "owner_method": "execute_delegated_read"
                },
                "issue_candidate_a1_review_grant": {
                    "owner_method": "issue_candidate_a1_review_grant"
                },
                "execute_candidate_a1_review": {
                    "owner_method": "execute_candidate_a1_review"
                },
                "open_complete_claim_admission_case": {
                    "owner_method": "open_complete_claim_admission_case"
                },
                "issue_admission_review_grant": {
                    "owner_method": "issue_admission_review_grant"
                },
                "issue_admission_decision_grant": {
                    "owner_method": "issue_admission_decision_grant"
                },
                "execute_admission_review": {
                    "owner_method": "execute_admission_review"
                },
                "execute_admission_decision": {
                    "owner_method": "execute_admission_decision"
                },
                "execute_formal_attempt": {
                    "owner_method": "formal_attempt_runtime"
                },
                "reconstruct": {"owner_method": "reconstruct"},
                "host_snapshot": {"owner_method": "host_snapshot"},
                "observe_mission": {"owner_method": "observe_mission"},
                "capture_native_material_observation": {
                    "owner_method": "capture_native_material_observation"
                },
                "fence_mission": {
                    "owner_method": "fence_mission_from_owner"
                },
                "record_direct_failed_executive_epoch": {
                    "owner_method": (
                        "record_direct_failed_executive_epoch_from_owner"
                    )
                },
            },
        )
        expected_operations = [
            "orient",
            "retrieve",
            "record_context",
            "interpret_material",
            "record_candidate",
            "record_branch",
            "synthesize",
            "record_strategy",
            "checkpoint",
        ]
        self.assertEqual(
            payload["model_projection"],
            TOOL.deep_thaw(TOOL.project_model_operations()),
        )
        self.assertEqual(
            payload["cli_projection"],
            TOOL.deep_thaw(TOOL.project_cli_operations()),
        )
        self.assertEqual(
            payload["model_projection"]["allowed_operations"],
            expected_operations,
        )
        self.assertFalse(payload["restrictions"]["direct_semantic_mutation"])
        mutating_usage = {
            item["name"]: item["side_effects"]
            for item in payload["verbs"]
            if item["name"] in TOOL._MUTATING_COMMANDS
        }
        self.assertEqual(set(mutating_usage), set(TOOL._MUTATING_COMMANDS))
        self.assertTrue(
            all(
                "direct execution is blocked" in value
                for value in mutating_usage.values()
            )
        )

        capabilities = self._run("--format", "json", "capabilities")
        self.assertEqual(capabilities.returncode, 0, capabilities.stderr)
        result = json.loads(capabilities.stdout)
        self.assertEqual(
            set(result["data"]["operations"]),
            set(expected_operations),
        )
        self.assertEqual(
            result["data"],
            TOOL.deep_thaw(TOOL.project_operation_capabilities()),
        )
        self.assertEqual(result["data"]["canonical_effect"], "none")


    def test_checkpoint_has_no_model_authored_owner_or_disposition_arguments(
        self,
    ) -> None:
        accepted = TOOL.validate_semantic_request(_request("checkpoint", {}))
        self.assertEqual(accepted["input"], {})
        model_projection = TOOL.deep_thaw(TOOL.project_model_operations())
        checkpoint_schema = model_projection["usage"]["operation_guides"][
            "checkpoint"
        ]["input_schema"]
        self.assertEqual(
            checkpoint_schema,
            {
                "type": "object",
                "additionalProperties": False,
                "properties": {},
                "required": [],
            },
        )
        for obsolete in (
            {"disposition": "stop"},
            {"strongest_evidence": "evidence.1"},
            {"next_epoch_objective": "continue the strongest route"},
        ):
            with self.subTest(obsolete=obsolete), self.assertRaises(
                TOOL.MissionOperationContractError
            ):
                TOOL.validate_semantic_request(_request("checkpoint", obsolete))

    def test_host_bridge_transports_empty_checkpoint_and_owner_result_unchanged(
        self,
    ) -> None:
        epoch, epoch_id = _epoch_projection()
        request = _request("checkpoint", {})
        expected = {
            "schema_version": TOOL.MISSION_SEMANTIC_RESULT_SCHEMA_VERSION,
            "operation": "checkpoint",
            "status": "completed",
            "result": {
                "checkpoint_id": "checkpoint:abc",
                "executive_epoch_id": epoch_id,
                "state": "checkpointed",
            },
            "error": None,
        }
        calls: list[object] = []

        def checkpoint(
            value: object,
            *,
            executive_epoch_id: str,
        ) -> dict[str, object]:
            calls.append(
                {
                    "request": value,
                    "executive_epoch_id": executive_epoch_id,
                }
            )
            return expected

        facade = SimpleNamespace(
            current_epoch=lambda: epoch,
            execute_semantic_operation=checkpoint,
        )
        payload = {
            "request": request,
            "binding": {
                "rootThreadId": "thread.root",
                "executiveEpochId": epoch_id,
            },
        }
        exit_code, result = self._run_bridge(
            _bridge_request("execute_semantic_operation", payload),
            facade,
        )
        self.assertEqual(exit_code, 0, result)
        self.assertEqual(result["data"], expected)
        self.assertEqual(
            calls,
            [{"request": request, "executive_epoch_id": epoch_id}],
        )

        epoch["state"] = "closed"
        exit_code, result = self._run_bridge(
            _bridge_request("execute_semantic_operation", payload),
            facade,
        )
        self.assertEqual(exit_code, 0, result)
        self.assertEqual(result["data"], expected)
        self.assertEqual(len(calls), 2)

        epoch["state"] = "mission_fenced"
        exit_code, result = self._run_bridge(
            _bridge_request("execute_semantic_operation", payload),
            facade,
        )
        self.assertEqual(exit_code, 0, result)
        self.assertEqual(result["data"], expected)
        self.assertEqual(len(calls), 3)

        non_checkpoint = {
            **payload,
            "request": _request("orient", {}),
        }
        exit_code, result = self._run_bridge(
            _bridge_request("execute_semantic_operation", non_checkpoint),
            facade,
        )
        self.assertEqual(exit_code, 1, result)
        self.assertEqual(
            result["errors"][0]["code"], "mission_host_bridge_binding_mismatch"
        )
        self.assertEqual(len(calls), 3)

        payload_with_host_authored_pending = {
            **payload,
            "pendingCaptureLocators": [],
        }
        exit_code, result = self._run_bridge(
            _bridge_request(
                "execute_semantic_operation",
                payload_with_host_authored_pending,
            ),
            facade,
        )
        self.assertEqual(exit_code, 1, result)
        self.assertEqual(
            result["errors"][0]["code"], "mission_host_bridge_request_invalid"
        )
        self.assertEqual(len(calls), 3)

    def test_host_bridge_projects_only_bounded_checkpoint_source_warning(self) -> None:
        from research_core.observability import (
            EventKind,
            ObservationEnvironment,
            ObservationLevel,
            StructuredEvent,
        )

        epoch, epoch_id = _epoch_projection()
        expected = {
            "schema_version": TOOL.MISSION_SEMANTIC_RESULT_SCHEMA_VERSION,
            "operation": "checkpoint",
            "status": "completed",
            "result": {
                "checkpoint_id": "checkpoint:warning-fixture",
                "executive_epoch_id": epoch_id,
                "state": "checkpointed",
            },
            "error": None,
        }
        captured_observer: object | None = None

        def opened(*_args: object, **kwargs: object) -> object:
            nonlocal captured_observer
            captured_observer = kwargs["observer"]

            def checkpoint(
                _request: object, *, executive_epoch_id: str
            ) -> dict[str, object]:
                assert executive_epoch_id == epoch_id
                captured_observer.emit_event(  # type: ignore[union-attr]
                    StructuredEvent(
                        kind=EventKind.BACKUP_CHANGED,
                        occurred_at="2026-09-10T12:00:00Z",
                        level=ObservationLevel.WARN,
                        environment=ObservationEnvironment.PROD,
                        attributes={
                            "project_id": "project.rh",
                            "project_commit": 17,
                            "writer_epoch": 3,
                            "status": (
                                "checkpoint_source_capture_failed_writer_reacquired"
                            ),
                            "reason_code": "checkpoint_source_capture_failed",
                            "byte_count": 4096,
                            "duration_seconds": 0.007,
                        },
                    )
                )
                return expected

            return SimpleNamespace(
                current_epoch=lambda: epoch,
                execute_semantic_operation=checkpoint,
            )

        bridge = _bridge_request(
            "execute_semantic_operation",
            {
                "request": _request("checkpoint", {}),
                "binding": {
                    "rootThreadId": "thread.root",
                    "executiveEpochId": epoch_id,
                },
            },
        )
        with tempfile.TemporaryDirectory() as temporary:
            request_path = Path(temporary) / "bridge.json"
            response_path = Path(temporary) / "bridge-response.json"
            request_path.write_text(json.dumps(bridge), encoding="utf-8")
            with patch.object(TOOL.MissionInterface, "open", side_effect=opened):
                exit_code = TOOL.main(
                    [
                        "host-bridge",
                        "--workspace-root",
                        "C:\\rh-mission\\workspace",
                        "--project-id",
                        "project.rh",
                        "--mission-id",
                        "mission.1",
                        "--request",
                        str(request_path.resolve()),
                        "--response",
                        str(response_path.resolve()),
                        "--format",
                        "json",
                    ]
                )
            result = json.loads(response_path.read_text(encoding="utf-8"))

        self.assertEqual(exit_code, 0, result)
        self.assertEqual(result["data"], expected)
        self.assertEqual(
            result["warnings"],
            [
                {
                    "code": "checkpoint_source_capture_failed_writer_reacquired",
                    "reason_code": "checkpoint_source_capture_failed",
                    "database_bytes": 4096,
                    "handoff_elapsed_ms": 7,
                }
            ],
        )
        unsafe_observer = TOOL.CaptureWorkspaceObserver()
        unsafe_observer.emit_event(
            StructuredEvent(
                kind=EventKind.BACKUP_CHANGED,
                occurred_at="2026-09-10T12:00:01Z",
                level=ObservationLevel.WARN,
                environment=ObservationEnvironment.PROD,
                attributes={
                    "status": "checkpoint_source_capture_failed_writer_reacquired",
                    "reason_code": "checkpoint_source_capture_failed",
                    "byte_count": 1 << 53,
                    "duration_seconds": 0.007,
                },
            )
        )
        self.assertIsNone(TOOL._checkpoint_source_capture_warning(unsafe_observer))
        teardown_observer = TOOL.CaptureWorkspaceObserver()
        teardown_observer.emit_event(
            StructuredEvent(
                kind=EventKind.BACKUP_CHANGED,
                occurred_at="2026-09-12T12:00:02Z",
                level=ObservationLevel.WARN,
                environment=ObservationEnvironment.PROD,
                attributes={
                    "status": (
                        "checkpoint_source_operation_teardown_failed_"
                        "writer_revalidated"
                    ),
                    "reason_code": "checkpoint_source_published",
                    "byte_count": 4096,
                    "duration_seconds": 0.009,
                },
            )
        )
        self.assertEqual(
            TOOL._checkpoint_source_capture_warning(teardown_observer),
            {
                "code": (
                    "checkpoint_source_operation_teardown_failed_"
                    "writer_revalidated"
                ),
                "reason_code": "checkpoint_source_published",
                "database_bytes": 4096,
                "handoff_elapsed_ms": 9,
            },
        )

    def test_host_bridge_preserves_exact_checkpoint_source_failure_code(self) -> None:
        epoch, epoch_id = _epoch_projection()
        bridge = _bridge_request(
            "execute_semantic_operation",
            {
                "request": _request("checkpoint", {}),
                "binding": {
                    "rootThreadId": "thread.root",
                    "executiveEpochId": epoch_id,
                },
            },
        )
        failures = (
            TOOL.CheckpointSourceError(
                "checkpoint_source_publication_failed",
                "checkpoint source publication failed",
            ),
            TOOL.CheckpointSourceReacquisitionError(
                "checkpoint source writer reacquisition failed",
                capture_failure_code="checkpoint_source_publication_failed",
                published_source=None,
            ),
        )
        for failure in failures:
            with self.subTest(code=failure.code):
                facade = SimpleNamespace(
                    current_epoch=lambda: epoch,
                    execute_semantic_operation=Mock(side_effect=failure),
                )
                exit_code, result = self._run_bridge(bridge, facade)
                self.assertEqual(exit_code, 1, result)
                self.assertEqual(result["warnings"], [])
                self.assertEqual(result["errors"][0]["code"], failure.code)

    def test_host_bridge_projects_busy_checkpoint_source_as_success_warning(
        self,
    ) -> None:
        from research_core.observability import (
            EventKind,
            ObservationEnvironment,
            ObservationLevel,
            StructuredEvent,
        )

        epoch, epoch_id = _epoch_projection()
        expected = {
            "schema_version": TOOL.MISSION_SEMANTIC_RESULT_SCHEMA_VERSION,
            "operation": "checkpoint",
            "status": "completed",
            "result": {
                "checkpoint_id": "checkpoint:busy-warning-fixture",
                "executive_epoch_id": epoch_id,
                "state": "checkpointed",
            },
            "error": None,
        }

        def opened(*_args: object, **kwargs: object) -> object:
            observer = kwargs["observer"]

            def checkpoint(
                _request: object, *, executive_epoch_id: str
            ) -> dict[str, object]:
                assert executive_epoch_id == epoch_id
                observer.emit_event(
                    StructuredEvent(
                        kind=EventKind.BACKUP_CHANGED,
                        occurred_at="2026-09-12T12:00:00Z",
                        level=ObservationLevel.WARN,
                        environment=ObservationEnvironment.PROD,
                        attributes={
                            "project_id": "project.rh",
                            "project_commit": 18,
                            "writer_epoch": 4,
                            "status": (
                                "checkpoint_source_snapshot_skipped_writer_preserved"
                            ),
                            "reason_code": "checkpoint_source_busy",
                            "duration_seconds": 0.003,
                        },
                    )
                )
                return expected

            return SimpleNamespace(
                current_epoch=lambda: epoch,
                execute_semantic_operation=checkpoint,
            )

        bridge = _bridge_request(
            "execute_semantic_operation",
            {
                "request": _request("checkpoint", {}),
                "binding": {
                    "rootThreadId": "thread.root",
                    "executiveEpochId": epoch_id,
                },
            },
        )
        with tempfile.TemporaryDirectory() as temporary:
            request_path = Path(temporary) / "bridge.json"
            response_path = Path(temporary) / "bridge-response.json"
            request_path.write_text(json.dumps(bridge), encoding="utf-8")
            with patch.object(TOOL.MissionInterface, "open", side_effect=opened):
                exit_code = TOOL.main(
                    [
                        "host-bridge",
                        "--workspace-root",
                        "C:\\rh-mission\\workspace",
                        "--project-id",
                        "project.rh",
                        "--mission-id",
                        "mission.1",
                        "--request",
                        str(request_path.resolve()),
                        "--response",
                        str(response_path.resolve()),
                        "--format",
                        "json",
                    ]
                )
            result = json.loads(response_path.read_text(encoding="utf-8"))

        self.assertEqual(exit_code, 0, result)
        self.assertEqual(result["data"], expected)
        self.assertEqual(
            result["warnings"],
            [
                {
                    "code": "checkpoint_source_snapshot_skipped_writer_preserved",
                    "reason_code": "checkpoint_source_busy",
                    "database_bytes": None,
                    "handoff_elapsed_ms": 3,
                }
            ],
        )

    def test_host_bridge_reports_committed_checkpoint_on_unsafe_handoff(
        self,
    ) -> None:
        epoch, epoch_id = _epoch_projection()
        completed = {
            "schema_version": TOOL.MISSION_SEMANTIC_RESULT_SCHEMA_VERSION,
            "operation": "checkpoint",
            "status": "completed",
            "result": {
                "checkpoint_id": "checkpoint:unsafe-handoff-fixture",
                "executive_epoch_id": epoch_id,
                "state": "checkpointed",
            },
            "error": None,
        }
        failure = TOOL.CheckpointCommittedSourceHandoffError(
            semantic_result=completed,
            cause=TOOL.CheckpointSourceReacquisitionError(
                "checkpoint source writer reacquisition failed",
                capture_failure_code="checkpoint_source_publication_failed",
                published_source=None,
            ),
        )
        facade = SimpleNamespace(
            current_epoch=lambda: epoch,
            execute_semantic_operation=Mock(side_effect=failure),
        )
        exit_code, result = self._run_bridge(
            _bridge_request(
                "execute_semantic_operation",
                {
                    "request": _request("checkpoint", {}),
                    "binding": {
                        "rootThreadId": "thread.root",
                        "executiveEpochId": epoch_id,
                    },
                },
            ),
            facade,
        )

        self.assertEqual(exit_code, 1, result)
        self.assertEqual(result["status"], "error")
        self.assertTrue(result["data"]["checkpoint_completed"])
        self.assertEqual(result["data"]["checkpoint"], completed["result"])
        self.assertEqual(result["data"]["canonical_effect"], "none")
        self.assertEqual(result["data"]["mathematical_effect"], "none")
        self.assertEqual(result["data"]["continuation_safety"], "unsafe")
        self.assertEqual(result["data"]["source_publication_state"], "unknown")
        self.assertEqual(
            result["data"]["source_handoff_failure_code"],
            "checkpoint_source_reacquisition_failed",
        )
        self.assertEqual(
            result["errors"][0]["code"],
            "checkpoint_committed_source_handoff_failed",
        )
        self.assertEqual(
            result["errors"][0]["reason_code"],
            "checkpoint_source_reacquisition_failed",
        )
        self.assertTrue(result["errors"][0]["checkpoint_completed"])
        self.assertEqual(
            result["errors"][0]["checkpoint_id"],
            completed["result"]["checkpoint_id"],
        )

        unverified = TOOL.CheckpointCommittedSourceHandoffError(
            semantic_result=completed,
            cause=TOOL.CheckpointSourceError(
                "checkpoint_source_writer_state_unverified",
                "writer release outcome could not be verified",
            ),
        )
        unverified_facade = SimpleNamespace(
            current_epoch=lambda: epoch,
            execute_semantic_operation=Mock(side_effect=unverified),
        )
        exit_code, result = self._run_bridge(
            _bridge_request(
                "execute_semantic_operation",
                {
                    "request": _request("checkpoint", {}),
                    "binding": {
                        "rootThreadId": "thread.root",
                        "executiveEpochId": epoch_id,
                    },
                },
            ),
            unverified_facade,
        )
        self.assertEqual(exit_code, 1, result)
        self.assertEqual(result["status"], "error")
        self.assertTrue(result["data"]["checkpoint_completed"])
        self.assertEqual(result["data"]["checkpoint"], completed["result"])
        self.assertEqual(result["data"]["continuation_safety"], "unverified")
        self.assertEqual(
            result["data"]["source_handoff_failure_code"],
            "checkpoint_source_writer_state_unverified",
        )

        safe_cause = TOOL.CheckpointSourceError(
            "checkpoint_source_invalid",
            "published source failed exact validation after safe writer reacquisition",
        )
        safe_cause.writer_reacquired = True
        safe_cause.published_source = object()
        safe = TOOL.CheckpointCommittedSourceHandoffError(
            semantic_result=completed,
            cause=safe_cause,
        )
        safe_facade = SimpleNamespace(
            current_epoch=lambda: epoch,
            execute_semantic_operation=Mock(side_effect=safe),
        )
        exit_code, result = self._run_bridge(
            _bridge_request(
                "execute_semantic_operation",
                {
                    "request": _request("checkpoint", {}),
                    "binding": {
                        "rootThreadId": "thread.root",
                        "executiveEpochId": epoch_id,
                    },
                },
            ),
            safe_facade,
        )
        self.assertEqual(exit_code, 1, result)
        self.assertEqual(result["status"], "error")
        self.assertTrue(result["data"]["checkpoint_completed"])
        self.assertEqual(result["data"]["checkpoint"], completed["result"])
        self.assertEqual(result["data"]["continuation_safety"], "safe")
        self.assertEqual(result["data"]["source_publication_state"], "published")
        self.assertEqual(
            result["data"]["source_handoff_failure_code"],
            "checkpoint_source_invalid",
        )

    def test_host_bridge_retains_exact_semantic_validation_location(self) -> None:
        epoch, epoch_id = _epoch_projection()
        calls: list[object] = []
        facade = SimpleNamespace(
            current_epoch=lambda: epoch,
            execute_semantic_operation=lambda request, *, executive_epoch_id: calls.append(
                (request, executive_epoch_id)
            ),
        )
        payload = {
            "request": _request(
                "record_candidate",
                {
                    "candidate_id": "candidate:missing-standing-basis",
                    "proposal_kind": "lemma",
                    "exact_statement": "A bounded candidate statement.",
                    "standing": {"status": "open", "basis": ""},
                },
            ),
            "binding": {
                "rootThreadId": "thread.root",
                "executiveEpochId": epoch_id,
            },
        }

        exit_code, result = self._run_bridge(
            _bridge_request("execute_semantic_operation", payload),
            facade,
        )

        self.assertEqual(exit_code, 1)
        self.assertEqual(calls, [])
        self.assertEqual(
            result["errors"][0]["code"], "mission_operation_request_invalid"
        )
        self.assertEqual(
            result["errors"][0]["location"], "$.input.standing.basis"
        )
        self.assertIn("targeted usage guide", result["errors"][0]["retry_hint"])

    def test_host_bridge_rejects_record_context_retrieval_handles_at_exact_field(self) -> None:
        epoch, epoch_id = _epoch_projection()
        calls: list[object] = []
        facade = SimpleNamespace(
            current_epoch=lambda: epoch,
            execute_semantic_operation=lambda request, *, executive_epoch_id: calls.append(
                (request, executive_epoch_id)
            ),
        )
        payload = {
            "request": _request(
                "record_context",
                {
                    "context_id": "context:shared-research-question",
                    "subject": "One exact retained question.",
                    "question": "Which obstruction survives?",
                    "material": [
                        {
                            "id": "evidence:current-obstruction@1",
                            "why": "A retrieval handle is not a current-owner id.",
                        }
                    ],
                },
            ),
            "binding": {
                "rootThreadId": "thread.root",
                "executiveEpochId": epoch_id,
            },
        }

        exit_code, result = self._run_bridge(
            _bridge_request("execute_semantic_operation", payload),
            facade,
        )

        self.assertEqual(exit_code, 1)
        self.assertEqual(calls, [])
        self.assertEqual(
            result["errors"][0]["code"], "mission_operation_request_invalid"
        )
        self.assertEqual(
            result["errors"][0]["location"], "$.input.material[0].id"
        )
        self.assertIn("targeted usage guide", result["errors"][0]["retry_hint"])

    def test_host_binding_reads_only_direct_current_epoch_facts(self) -> None:
        epoch, epoch_id = _epoch_projection()
        binding = TOOL._current_epoch_binding(
            SimpleNamespace(current_epoch=lambda: epoch)
        )
        self.assertEqual(
            binding,
            {
                "state": "open",
                "rootThreadId": "thread.root",
                "executiveEpochId": epoch_id,
                "workspaceRoot": "C:\\rh-mission\\workspace",
            },
        )

        stopped, _handle = _epoch_projection(state="closed")
        with self.assertRaisesRegex(
            TOOL.MissionInterfaceError,
            "not bound to an allowed current executive epoch",
        ):
            TOOL._current_epoch_binding(SimpleNamespace(current_epoch=lambda: stopped))

    def test_direct_lifecycle_actions_reject_legacy_aliases_and_open_shapes(
        self,
    ) -> None:
        facade = SimpleNamespace()
        invalid_requests = (
            _bridge_request(
                "open_executive_epoch",
                {
                    "role": "predecessor",
                    "openingDisposition": "initial",
                    "rootThreadId": "thread.root",
                    "objective": "Legacy opening prose.",
                    "workspaceRoot": "C:\\rh-mission\\workspace",
                },
            ),
            _bridge_request("authorize_executive_epoch", {"role": "host"}),
            _bridge_request(
                "bind_executive_epoch",
                {
                    "executiveEpochId": "epoch.1",
                    "rootThreadId": "thread.root",
                },
            ),
            _bridge_request(
                "record_failed_executive_epoch",
                {
                    "context": {"reason": "legacy"},
                    "binding": {
                        "role": "predecessor",
                        "executiveEpochId": "epoch.1",
                    },
                },
            ),
            _bridge_request(
                "record_direct_failed_executive_epoch",
                {
                    "executiveEpochId": "epoch.1",
                    "reconciliation": {},
                },
            ),
        )
        for request in invalid_requests:
            with self.subTest(action=request["action"]):
                exit_code, result = self._run_bridge(request, facade)
                self.assertEqual(exit_code, 1, result)
                self.assertEqual(
                    result["errors"][0]["code"],
                    "mission_host_bridge_request_invalid",
                )

    def test_direct_failure_remains_dispatchable_after_mission_fence(
        self,
    ) -> None:
        epoch, epoch_id = _epoch_projection(state="mission_fenced")
        reconciliation = {
            "stage": "goal_runtime",
            "failure_reason": "goal ended without a checkpoint",
        }
        calls: list[object] = []
        facade = SimpleNamespace(
            current_epoch=lambda: epoch,
            record_direct_failed_executive_epoch_from_owner=lambda value: (
                calls.append(value)
                or {
                    "executive_epoch_id": epoch_id,
                    "state": "failed_before_checkpoint",
                }
            ),
            execute_semantic_operation=lambda _request, *, executive_epoch_id: {},
        )
        failed_payload = {
            "executiveEpochId": epoch_id,
            "reconciliation": reconciliation,
        }
        exit_code, result = self._run_bridge(
            _bridge_request(
                "record_direct_failed_executive_epoch",
                failed_payload,
            ),
            facade,
        )
        self.assertEqual(exit_code, 0, result)
        self.assertEqual(calls, [failed_payload])
        self.assertEqual(
            result["data"],
            {
                "executive_epoch_id": epoch_id,
                "state": "failed_before_checkpoint",
            },
        )

        semantic_payload = {
            "request": _request("orient", {}),
            "binding": {
                "rootThreadId": "thread.root",
                "executiveEpochId": epoch_id,
            },
        }
        exit_code, result = self._run_bridge(
            _bridge_request("execute_semantic_operation", semantic_payload),
            facade,
        )
        self.assertEqual(exit_code, 1, result)
        self.assertEqual(
            result["errors"][0]["code"],
            "mission_host_bridge_binding_mismatch",
        )

    def test_mission_fence_lost_response_remains_dispatchable(self) -> None:
        epoch, epoch_id = _epoch_projection(state="mission_fenced")
        calls: list[object] = []

        def fence(
            value: object,
            *,
            executive_epoch_id: str,
        ) -> dict[str, object]:
            calls.append(
                {
                    "context": value,
                    "executive_epoch_id": executive_epoch_id,
                }
            )
            return {"mission_id": "mission.1", "state": "fenced"}

        context = {
            "threadId": "thread.root",
            "reason": "unknown_effect",
            "containmentScope": "mission_fence",
        }
        facade = SimpleNamespace(
            current_epoch=lambda: epoch,
            fence_mission_from_owner=fence,
        )
        exit_code, result = self._run_bridge(
            _bridge_request(
                "fence_mission",
                {
                    "context": context,
                    "binding": {"executiveEpochId": epoch_id},
                },
            ),
            facade,
        )
        self.assertEqual(exit_code, 0, result)
        self.assertEqual(
            result["data"],
            {"mission_id": "mission.1", "state": "fenced"},
        )
        self.assertEqual(
            calls,
            [{"context": context, "executive_epoch_id": epoch_id}],
        )

    def test_mission_fence_preserves_fixed_failure_details_without_error_text(self) -> None:
        epoch, epoch_id = _epoch_projection()
        private_text = "UNRESTRICTED_ERROR_TEXT_MUST_NOT_ENTER_SAFE_CODES"
        cases = (
            (TOOL.StaleCommandError(private_text), "owner", "stale_command", None),
            (TOOL.CommandConflictError(private_text), "store", "command_conflict", None),
            (TypeError(private_text), "owner", "type", None),
            (ValueError(private_text), "store", "value", None),
            (
                TOOL.MissionOwnerError("mission_contract_invalid", private_text, private_text),
                "owner", "mission_owner", "mission_fence_mission_contract_invalid",
            ),
            (
                TOOL.MissionOwnerError(private_text, private_text, private_text),
                "owner", "mission_owner", None,
            ),
            (
                TOOL.MissionFenceStaleCommandError(
                    private_text,
                    diagnostic_code="mission_fence_store_authorized_root_stale",
                ),
                "store", "stale_command", "mission_fence_store_authorized_root_stale",
            ),
        )
        for error, stage, category, condition in cases:
            with self.subTest(stage=stage, category=category, condition=condition):
                def fence(*_args: object, **_kwargs: object) -> object:
                    with TOOL.mission_fence_diagnostic_stage(stage):
                        raise error

                facade = SimpleNamespace(
                    current_epoch=lambda: epoch, fence_mission_from_owner=fence
                )
                exit_code, result = self._run_bridge(
                    _bridge_request("fence_mission", {
                        "context": {
                            "threadId": "thread.root", "reason": "mission_consistency_failure",
                            "containmentScope": "mission_fence",
                        },
                        "binding": {"executiveEpochId": epoch_id},
                    }),
                    facade,
                )
                self.assertEqual(exit_code, 2, result)
                codes = [item["code"] for item in result["errors"]]
                expected = [
                    "invalid_invocation", f"mission_fence_stage_{stage}",
                    f"mission_fence_error_{category}",
                ]
                if condition is not None:
                    expected.append(condition)
                self.assertEqual(codes, expected)
                self.assertNotIn(private_text, json.dumps(result["errors"][1:]))
                self.assertNotIn(private_text, json.dumps(codes))
                # The original protected tool response and exception are unchanged.
                self.assertEqual(result["errors"][0]["message"], private_text)
                self.assertEqual(str(error), private_text)

        with self.assertRaises(ValueError):
            TOOL.MissionFenceStaleCommandError(private_text, diagnostic_code=private_text)

    def test_mission_fence_host_binding_stage_remains_fail_closed(self) -> None:
        epoch, epoch_id = _epoch_projection(state="closed")
        fence = unittest.mock.Mock()
        exit_code, result = self._run_bridge(
            _bridge_request("fence_mission", {
                "context": {
                    "threadId": "thread.root", "reason": "mission_consistency_failure",
                    "containmentScope": "mission_fence",
                },
                "binding": {"executiveEpochId": epoch_id},
            }),
            SimpleNamespace(current_epoch=lambda: epoch, fence_mission_from_owner=fence),
        )
        self.assertEqual(exit_code, 1, result)
        self.assertEqual([item["code"] for item in result["errors"]], [
            "mission_host_bridge_binding_mismatch",
            "mission_fence_stage_host_binding", "mission_fence_error_mission_interface",
        ])
        fence.assert_not_called()

    def test_non_fence_failure_has_no_fence_diagnostic_codes(self) -> None:
        def fail_reconstruct() -> object:
            raise ValueError("ordinary diagnostic")

        exit_code, result = self._run_bridge(
            _bridge_request("reconstruct", {}),
            SimpleNamespace(reconstruct=fail_reconstruct),
        )
        self.assertEqual(exit_code, 2, result)
        self.assertEqual([item["code"] for item in result["errors"]], ["invalid_invocation"])

    def test_owner_model_migration_validates_closed_request_before_owner_call(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            request_path = root / "migration.json"
            request = {
                "schema_version": "mathematical_research.mission_model_policy_migration_request.v1",
                "project_id": "project.rh", "mission_id": "mission.1",
                "workspace_root": str(root), "target_release_sha": "a" * 40,
                "expected_mission_revision": 2,
                "expected_mission_payload_sha256": "b" * 64,
                "expected_canonical_authority_digest": "c" * 64,
            }
            calls = []
            def migrate(**kwargs):
                calls.append(kwargs)
                return {"schema_version": "mathematical_research.mission_model_policy_migration_result.v1", "model": "gpt-6-astra"}
            facade = SimpleNamespace(migrate_model_policy_from_owner=migrate)
            argv = ["owner-migrate-model-policy", "--workspace-root", str(root),
                    "--project-id", "project.rh", "--mission-id", "mission.1",
                    "--request", str(request_path), "--format", "json"]
            mutations = [None, {"extra": True}, {"project_id": "other"},
                         {"mission_id": "other"}, {"workspace_root": "relative"},
                         {"target_release_sha": "d" * 40}, {"expected_mission_revision": True},
                         {"expected_mission_revision": 0}, {"expected_mission_payload_sha256": "x" * 64},
                         {"expected_canonical_authority_digest": "c" * 64 + "\n"}]
            for mutation in mutations:
                with self.subTest(mutation=mutation):
                    request_path.write_text(json.dumps({**request, **(mutation or {})}), encoding="utf-8")
                    output = io.StringIO()
                    with patch.object(TOOL, "_read_owner_release_commit", return_value="a" * 40), \
                         patch.object(TOOL.MissionInterface, "open", autospec=True, return_value=facade) as opened, \
                         patch.object(TOOL, "load_canonical_snapshot") as canonical_load, \
                         redirect_stdout(output):
                        code = TOOL.main(argv)
                    result = json.loads(output.getvalue())
                    canonical_load.assert_not_called()
                    if mutation is None:
                        self.assertEqual(code, 0, result)
                        self.assertEqual(result["verb"], "owner-migrate-model-policy")
                        opened.assert_called_once_with(root, expected_project_id="project.rh", expected_mission_id="mission.1")
                    else:
                        self.assertNotEqual(code, 0, result)
                        opened.assert_not_called()
            self.assertEqual(calls, [{
                "expected_mission_revision": 2,
                "expected_mission_payload_sha256": "b" * 64,
                "expected_canonical_authority_digest": "c" * 64,
            }])
            self.assertNotIn("owner-migrate-model-policy", TOOL._HOST_BRIDGE_ACTIONS)

    def test_owner_reauthorization_validates_closed_incident_request_before_owner_call(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            other_root = root / "other"
            other_root.mkdir()
            request_path = root / "reauthorization.json"
            request = {
                "schema_version": "mathematical_research.mission_reauthorization_request.v1",
                "project_id": "project.rh", "mission_id": "mission.1",
                "workspace_root": str(root), "target_release_sha": "a" * 40,
                "expected_mission_revision": 2,
                "expected_mission_payload_sha256": "b" * 64,
                "executive_epoch_id": "epoch.incident", "root_thread_id": "thread.incident",
                "expected_terminal_event_sha256": "d" * 64,
                "expected_canonical_authority_digest": "c" * 64,
            }
            owner_result = {
                "schema_version": "mathematical_research.mission_reauthorization_result.v1",
                "idempotent": True,
                "source_mission_ref": {"revision": 2, "payload_sha256": "b" * 64},
                "target_mission_ref": {"revision": 3, "payload_sha256": "e" * 64},
            }
            reauthorize = unittest.mock.Mock(return_value=owner_result)
            facade = SimpleNamespace(reauthorize_mission_from_owner=reauthorize)
            argv = ["owner-reauthorize-mission", "--workspace-root", str(root),
                    "--project-id", "project.rh", "--mission-id", "mission.1",
                    "--request", str(request_path), "--format", "json"]
            mutations = [
                {"extra": True}, {"schema_version": "unrecognized.v1"},
                {"project_id": "other"}, {"mission_id": "other"},
                {"workspace_root": "relative"}, {"workspace_root": str(other_root)},
                {"target_release_sha": "f" * 40}, {"target_release_sha": "A" * 40},
                {"expected_mission_revision": True}, {"expected_mission_revision": 0},
                {"expected_mission_revision": "2"},
                {"expected_mission_payload_sha256": "x" * 64},
                {"executive_epoch_id": ""}, {"executive_epoch_id": " epoch.incident"},
                {"root_thread_id": None}, {"root_thread_id": "thread.incident\n"},
                {"expected_terminal_event_sha256": "D" * 64},
                {"expected_terminal_event_sha256": 7},
                {"expected_canonical_authority_digest": "c" * 64 + "\n"},
            ]
            cases = [("valid", request)]
            cases.extend((str(mutation), {**request, **mutation}) for mutation in mutations)
            cases.extend(
                (f"missing {name}", {key: value for key, value in request.items() if key != name})
                for name in request
            )
            for label, payload in cases:
                with self.subTest(case=label):
                    request_path.write_text(json.dumps(payload), encoding="utf-8")
                    output = io.StringIO()
                    with patch.object(TOOL, "_read_owner_release_commit", return_value="a" * 40), \
                         patch.object(TOOL.MissionInterface, "open", autospec=True, return_value=facade) as opened, \
                         patch.object(TOOL, "load_canonical_snapshot") as canonical_load, \
                         redirect_stdout(output):
                        code = TOOL.main(argv)
                    result = json.loads(output.getvalue())
                    canonical_load.assert_not_called()
                    if label == "valid":
                        self.assertEqual(code, 0, result)
                        self.assertEqual(result["verb"], "owner-reauthorize-mission")
                        self.assertEqual(result["data"], owner_result)
                        opened.assert_called_once_with(root, expected_project_id="project.rh", expected_mission_id="mission.1")
                    else:
                        self.assertEqual(code, 2, result)
                        self.assertEqual([item["code"] for item in result["errors"]], ["invalid_invocation"])
                        opened.assert_not_called()
            reauthorize.assert_called_once_with(
                expected_mission_revision=2,
                expected_mission_payload_sha256="b" * 64,
                executive_epoch_id="epoch.incident",
                root_thread_id="thread.incident",
                expected_terminal_event_sha256="d" * 64,
                expected_canonical_authority_digest="c" * 64,
            )
            self.assertNotIn("owner-reauthorize-mission", TOOL._HOST_BRIDGE_ACTIONS)
            self.assertNotIn("owner-reauthorize-mission", [item["verb"] for item in TOOL._CLI_PROJECTION["verbs"]])

    def test_owner_scientific_context_binding_validates_before_owner_access(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            other_root = root / "other"
            other_root.mkdir()
            request_path = root / "scientific-context-binding.json"
            request = {
                "schema_version": "mathematical_research.mission_scientific_context_binding_request.v1",
                "project_id": "project.rh",
                "mission_id": "mission.1",
                "workspace_root": str(root),
                "target_release_sha": "a" * 40,
                "expected_mission_revision": 2,
                "expected_mission_payload_sha256": "b" * 64,
                "expected_canonical_authority_digest": "c" * 64,
                "context_id": "science.rh",
                "expected_context_revision": 4,
                "expected_context_payload_sha256": "d" * 64,
            }
            owner_result = {
                "schema_version": "mathematical_research.mission_scientific_context_binding_result.v1",
                "idempotent": True,
                "source_mission_ref": {"revision": 2, "payload_sha256": "b" * 64},
                "target_mission_ref": {"revision": 3, "payload_sha256": "e" * 64},
                "scientific_context_ref": {
                    "context_id": "science.rh", "revision": 4, "payload_sha256": "d" * 64,
                },
            }
            bind = Mock(return_value=owner_result)
            facade = SimpleNamespace(bind_scientific_context_from_owner=bind)
            argv = [
                "owner-bind-scientific-context", "--workspace-root", str(root),
                "--project-id", "project.rh", "--mission-id", "mission.1",
                "--request", str(request_path), "--format", "json",
            ]
            mutations = [
                {"extra": True}, {"schema_version": "unrecognized.v1"},
                {"project_id": "other"}, {"mission_id": "other"},
                {"workspace_root": "relative"}, {"workspace_root": str(other_root)},
                {"target_release_sha": "f" * 40}, {"target_release_sha": "A" * 40},
                {"expected_mission_revision": True}, {"expected_mission_revision": 0},
                {"expected_mission_revision": "2"},
                {"expected_mission_payload_sha256": "x" * 64},
                {"expected_mission_payload_sha256": "B" * 64},
                {"expected_canonical_authority_digest": "c" * 64 + "\n"},
                {"expected_context_revision": True}, {"expected_context_revision": 0},
                {"expected_context_revision": -1}, {"expected_context_revision": "4"},
                {"expected_context_payload_sha256": "D" * 64},
                {"expected_context_payload_sha256": "d" * 63},
                {"expected_context_payload_sha256": None},
                {"context_id": ""}, {"context_id": " science.rh"},
                {"context_id": "science.rh\n"}, {"context_id": "context:science.rh"},
                {"context_id": "science.rh@4"}, {"context_id": "Context title"},
                {"context_id": "science/rh"}, {"context_id": r"C:\science\rh"},
                {"context_id": None}, {"context_id": {"context_id": "science.rh"}},
            ]
            cases = [("valid", request)]
            cases.extend((str(mutation), {**request, **mutation}) for mutation in mutations)
            cases.extend(
                (f"missing {name}", {key: value for key, value in request.items() if key != name})
                for name in request
            )
            for label, payload in cases:
                with self.subTest(case=label):
                    request_path.write_text(json.dumps(payload), encoding="utf-8")
                    output = io.StringIO()
                    with patch.object(TOOL, "_read_owner_release_commit", return_value="a" * 40), \
                         patch.object(TOOL.MissionInterface, "open", autospec=True, return_value=facade) as opened, \
                         patch.object(TOOL, "load_canonical_snapshot") as canonical_load, \
                         redirect_stdout(output):
                        code = TOOL.main(argv)
                    result = json.loads(output.getvalue())
                    canonical_load.assert_not_called()
                    if label == "valid":
                        self.assertEqual(code, 0, result)
                        self.assertEqual(result["verb"], "owner-bind-scientific-context")
                        self.assertEqual(result["data"], owner_result)
                        opened.assert_called_once_with(
                            root, expected_project_id="project.rh", expected_mission_id="mission.1"
                        )
                    else:
                        self.assertEqual(code, 2, result)
                        self.assertEqual(
                            [item["code"] for item in result["errors"]], ["invalid_invocation"]
                        )
                        opened.assert_not_called()
            bind.assert_called_once_with(
                expected_mission_revision=2,
                expected_mission_payload_sha256="b" * 64,
                expected_canonical_authority_digest="c" * 64,
                context_id="science.rh",
                expected_context_revision=4,
                expected_context_payload_sha256="d" * 64,
            )
            self.assertNotIn("owner-bind-scientific-context", TOOL._HOST_BRIDGE_ACTIONS)
            self.assertNotIn(
                "owner-bind-scientific-context", [item["verb"] for item in TOOL._CLI_PROJECTION["verbs"]]
            )

    def test_owner_genesis_accepts_one_valid_productive_seed(self) -> None:
        source_commit = "a" * 40

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            workspace = (root / "mission").resolve()
            seed = json.loads(
                (REPO_ROOT / TOOL.MISSION_SEED_RELATIVE_PATH).read_text(
                    encoding="utf-8"
                )
            )
            seed_path = (root / "mission-seed.json").resolve()
            seed_path.write_text(json.dumps(seed), encoding="utf-8")
            release_record = root / "release.json"
            release_record.write_text(
                json.dumps(
                    {
                        "schema_version": TOOL.MISSION_RELEASE_RECORD_SCHEMA_VERSION,
                        "release_sha": source_commit,
                    }
                ),
                encoding="utf-8",
            )
            request_path = root / "owner-genesis.json"
            request_path.write_text(
                json.dumps(
                    {
                        "schema_version": TOOL.MISSION_OWNER_GENESIS_REQUEST_SCHEMA_VERSION,
                        "mission_id": "mission.rh.public.1",
                        "workspace_root": str(workspace),
                        "source_commit": source_commit,
                    }
                ),
                encoding="utf-8",
            )
            stream = io.StringIO()
            with (
                patch.object(TOOL, "MISSION_RELEASE_RECORD_PATH", release_record),
                patch.object(TOOL, "MISSION_SEED_RELATIVE_PATH", seed_path),
                redirect_stdout(stream),
            ):
                exit_code = TOOL.main(
                    [
                        "owner-genesis",
                        "--workspace-root",
                        str(workspace),
                        "--project-id",
                        "project.riemann_hypothesis",
                        "--mission-id",
                        "mission.rh.public.1",
                        "--request",
                        str(request_path.resolve()),
                        "--format",
                        "json",
                    ]
                )
            payload = json.loads(stream.getvalue())
            self.assertEqual(exit_code, 0, payload)
            self.assertEqual(payload["data"]["status"], "initialized")
            self.assertEqual(
                payload["data"],
                {
                    "status": "initialized",
                    "project_id": "project.riemann_hypothesis",
                    "mission_id": "mission.rh.public.1",
                    "workspace_root": str(workspace),
                    "source_commit": source_commit,
                },
            )
            reopened = TOOL.MissionInterface.open(
                workspace,
                "project.riemann_hypothesis",
                "mission.rh.public.1",
            )
            self.assertEqual(reopened.current_epoch()["state"], "not_opened")
            with reopened._store.snapshot_connection() as connection:
                mission_rows = tuple(
                    connection.execute(
                        "SELECT revision, payload_json FROM mission_revision "
                        "WHERE object_id = ? ORDER BY revision",
                        ("mission.rh.public.1",),
                    )
                )
                branch_rows = tuple(
                    connection.execute(
                        "SELECT revision, payload_json FROM branch_revision "
                        "WHERE object_id = ? ORDER BY revision",
                        ("branch.rh.public.1",),
                    )
                )
                strategy_rows = tuple(
                    connection.execute(
                        "SELECT revision, payload_json FROM strategy_revision "
                        "WHERE object_id = ? ORDER BY revision",
                        ("strategy.rh.public.1",),
                    )
                )
                aggregate_rows = connection.execute(
                    "SELECT COUNT(*) FROM mission_bundle_snapshot"
                ).fetchone()[0]
                writer_rows = connection.execute(
                    "SELECT COUNT(*) FROM writer_epoch"
                ).fetchone()[0]
                epoch_rows = connection.execute(
                    "SELECT COUNT(*) FROM executive_epoch_event"
                ).fetchone()[0]
                metadata = connection.execute(
                    "SELECT canonical_authority_json, canonical_authority_digest "
                    "FROM workspace_metadata WHERE singleton = 1"
                ).fetchone()
            self.assertEqual(tuple(row["revision"] for row in mission_rows), (1,))
            self.assertEqual(
                json.loads(mission_rows[0]["payload_json"]), seed["mission"]
            )
            self.assertEqual(tuple(row["revision"] for row in branch_rows), (1,))
            self.assertEqual(tuple(row["revision"] for row in strategy_rows), (1,))
            self.assertEqual(
                json.loads(branch_rows[0]["payload_json"]),
                seed["opening_branch"],
            )
            self.assertEqual(
                json.loads(strategy_rows[0]["payload_json"]), seed["strategy"]
            )
            self.assertEqual(aggregate_rows, 0)
            self.assertEqual(writer_rows, 1)
            self.assertEqual(epoch_rows, 0)
            authority = json.loads(metadata["canonical_authority_json"])
            self.assertEqual(
                set(authority),
                {
                    "binding_version",
                    "source_class",
                    "canonical_state_path",
                    "canonical_state_sha256",
                    "source_commit",
                },
            )
            self.assertEqual(authority["binding_version"], 2)
            self.assertEqual(authority["source_commit"], source_commit)
            self.assertEqual(
                hashlib.sha256(
                    metadata["canonical_authority_json"].encode("utf-8")
                ).hexdigest(),
                metadata["canonical_authority_digest"],
            )

    def test_request_loader_rejects_duplicate_keys_and_relative_paths(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            duplicate = root / "duplicate.json"
            duplicate.write_text('{"a": 1, "a": 2}', encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "duplicate JSON key"):
                TOOL._read_request(str(duplicate.resolve()))
            with self.assertRaisesRegex(ValueError, "absolute path"):
                TOOL._read_request("relative-request.json")

    def test_invalid_invocation_uses_tool_result_envelope(self) -> None:
        missing_workspace = self._run("orient", "--request", "missing", "--format", "json")
        self.assertEqual(missing_workspace.returncode, 2, missing_workspace.stderr)
        payload = json.loads(missing_workspace.stdout)
        self.assertEqual(payload["schema_version"], "tool_result.v1")
        self.assertEqual(payload["errors"][0]["code"], "invalid_invocation")

        rejected_old_verb = self._run("status", "--format", "json")
        self.assertEqual(rejected_old_verb.returncode, 2, rejected_old_verb.stderr)
        self.assertEqual(
            json.loads(rejected_old_verb.stdout)["errors"][0]["code"],
            "invalid_invocation",
        )

    def test_workspace_integrity_failure_is_not_a_local_input_rejection(self) -> None:
        def fail_reconstruct() -> object:
            raise TOOL.WorkspaceIntegrityError("shared Mission store is unavailable")

        facade = SimpleNamespace(reconstruct=fail_reconstruct)
        exit_code, payload = self._run_bridge(
            _bridge_request("reconstruct", {}),
            facade,
        )
        self.assertEqual(exit_code, 2)
        self.assertEqual(payload["errors"][0]["code"], "workspace_error")

    def test_direct_mutating_verbs_require_host_bridge_before_owner_or_file_access(
        self,
    ) -> None:
        with patch.object(
            TOOL.MissionInterface,
            "open",
            side_effect=AssertionError("direct mutation must not open the owner"),
        ) as opened:
            for verb in sorted(TOOL._MUTATING_COMMANDS):
                with self.subTest(verb=verb):
                    stream = io.StringIO()
                    with redirect_stdout(stream):
                        exit_code = TOOL.main(
                            [
                                verb,
                                "--workspace-root",
                                "C:\\rh-mission\\workspace",
                                "--project-id",
                                "project.rh",
                                "--mission-id",
                                "mission.1",
                                "--request",
                                "C:\\intentionally-not-read.json",
                                "--format",
                                "json",
                            ]
                        )
                    self.assertEqual(exit_code, 1)
                    payload = json.loads(stream.getvalue())
                    self.assertEqual(
                        payload["errors"][0]["code"],
                        "mission_host_bridge_required",
                    )
        opened.assert_not_called()

    def test_read_only_verb_cannot_smuggle_a_mutating_request(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "strategy.json"
            path.write_text(json.dumps(_strategy_request()), encoding="utf-8")
            with patch.object(
                TOOL.MissionInterface,
                "open",
                side_effect=AssertionError("mismatched request must not open the owner"),
            ) as opened:
                stream = io.StringIO()
                with redirect_stdout(stream):
                    exit_code = TOOL.main(
                        [
                            "orient",
                            "--workspace-root",
                            "C:\\rh-mission\\workspace",
                            "--project-id",
                            "project.rh",
                            "--mission-id",
                            "mission.1",
                            "--request",
                            str(path.resolve()),
                            "--format",
                            "json",
                        ]
                    )
        self.assertEqual(exit_code, 1)
        self.assertEqual(
            json.loads(stream.getvalue())["errors"][0]["code"],
            "mission_cli_operation_mismatch",
        )
        opened.assert_not_called()

    def test_orient_routes_through_generic_contract_adapter(self) -> None:
        expected = _orientation_result()
        request = _request("orient", {})
        facade = SimpleNamespace(execute_semantic_operation=lambda _request: expected)
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "orient.json"
            path.write_text(json.dumps(request), encoding="utf-8")
            with patch.object(TOOL.MissionInterface, "open", return_value=facade) as opened:
                stream = io.StringIO()
                with redirect_stdout(stream):
                    exit_code = TOOL.main(
                        [
                            "orient",
                            "--workspace-root",
                            "C:\\rh-mission\\workspace",
                            "--project-id",
                            "project.rh",
                            "--mission-id",
                            "mission.1",
                            "--request",
                            str(path.resolve()),
                            "--format",
                            "json",
                        ]
                    )
        self.assertEqual(exit_code, 0)
        self.assertEqual(json.loads(stream.getvalue())["data"], expected)
        opened.assert_called_once_with(
            "C:\\rh-mission\\workspace", "project.rh", "mission.1"
        )

    def test_mutation_preview_uses_contract_projected_adapter(self) -> None:
        expected = {
            "schema_version": "mathematical_research.mission_semantic_preview.v1",
            "operation": "record_strategy",
            "status": "validated",
            "would_effect": "noncanonical_mission_state",
            "canonical_effect": "none",
            "public_effect": "none",
            "provider_effect": "none",
        }
        request = _strategy_request()
        facade = SimpleNamespace(
            preview_semantic_operation=lambda _request: expected,
            semantic_preview_schema=TOOL.MissionInterface.semantic_preview_schema,
        )
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "strategy.json"
            path.write_text(json.dumps(request), encoding="utf-8")
            with patch.object(TOOL.MissionInterface, "open", return_value=facade):
                stream = io.StringIO()
                with redirect_stdout(stream):
                    exit_code = TOOL.main(
                        [
                            "record-strategy",
                            "--workspace-root",
                            "C:\\rh-mission\\workspace",
                            "--project-id",
                            "project.rh",
                            "--mission-id",
                            "mission.1",
                            "--request",
                            str(path.resolve()),
                            "--dry-run",
                            "--format",
                            "json",
                        ]
                    )
        self.assertEqual(exit_code, 0)
        self.assertEqual(json.loads(stream.getvalue())["data"], expected)

    def test_mutation_preview_rejects_completed_semantic_result_shape(self) -> None:
        completed = {
            "schema_version": TOOL.MISSION_SEMANTIC_RESULT_SCHEMA_VERSION,
            "operation": "record_strategy",
            "status": "completed",
            "result": {
                "record_handle": "strategy.2",
                "revision": 2,
                "semantic_summary": "A normal write result is not a preview.",
            },
            "error": None,
        }
        request = _strategy_request()
        facade = SimpleNamespace(
            preview_semantic_operation=lambda _request: completed,
            semantic_preview_schema=TOOL.MissionInterface.semantic_preview_schema,
        )
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "strategy.json"
            path.write_text(json.dumps(request), encoding="utf-8")
            with patch.object(TOOL.MissionInterface, "open", return_value=facade):
                stream = io.StringIO()
                with redirect_stdout(stream):
                    exit_code = TOOL.main(
                        [
                            "record-strategy",
                            "--workspace-root",
                            "C:\\rh-mission\\workspace",
                            "--project-id",
                            "project.rh",
                            "--mission-id",
                            "mission.1",
                            "--request",
                            str(path.resolve()),
                            "--dry-run",
                            "--format",
                            "json",
                        ]
                    )
        self.assertEqual(exit_code, 1)
        self.assertEqual(
            json.loads(stream.getvalue())["errors"][0]["code"],
            "mission_operation_result_invalid",
        )

    def test_host_bridge_dispatches_only_exact_owner_arguments(self) -> None:
        epoch, epoch_handle = _epoch_projection()
        calls: list[tuple[str, object]] = []
        orient_result = _orientation_result()

        class Facade:
            @staticmethod
            def current_epoch() -> dict[str, object]:
                return epoch

            @staticmethod
            def authorize_executive_epoch_from_owner(
                value: object,
            ) -> dict[str, object]:
                calls.append(("authorize", value))
                return {
                    "executive_epoch_id": epoch_handle,
                    "state": "authorized",
                }

            @staticmethod
            def validate_suspended_epoch_cut_from_owner(
                value: object,
            ) -> dict[str, object]:
                calls.append(("validate_suspended", value))
                return {
                    "executive_epoch_id": epoch_handle,
                    "root_thread_id": "thread.root",
                    "state": "bound",
                    "cut_validated": True,
                }

            @staticmethod
            def bind_executive_epoch_from_owner(value: object) -> dict[str, object]:
                calls.append(("bind", value))
                return {
                    "executive_epoch_id": epoch_handle,
                    "state": "bound",
                }

            @staticmethod
            def execute_semantic_operation(
                value: object, *, executive_epoch_id: str
            ) -> dict[str, object]:
                calls.append(
                    (
                        "execute",
                        {
                            "request": value,
                            "executive_epoch_id": executive_epoch_id,
                        },
                    )
                )
                return orient_result

            @staticmethod
            def reconstruct() -> dict[str, object]:
                calls.append(("reconstruct", None))
                return {"state": "reconstructed"}

            @staticmethod
            def capture_native_material_observation(
                value: object, *, executive_epoch_id: str
            ) -> dict[str, object]:
                calls.append(
                    (
                        "capture",
                        {
                            "observation": value,
                            "executive_epoch_id": executive_epoch_id,
                        },
                    )
                )
                return {"status": "captured"}

            @staticmethod
            def fence_mission_from_owner(
                value: object, *, executive_epoch_id: str
            ) -> dict[str, object]:
                calls.append(
                    (
                        "fence",
                        {
                            "context": value,
                            "executive_epoch_id": executive_epoch_id,
                        },
                    )
                )
                return {"status": "fenced"}

            @staticmethod
            def record_direct_failed_executive_epoch_from_owner(
                value: object,
            ) -> dict[str, object]:
                calls.append(("failed", value))
                return {
                    "executive_epoch_id": epoch_handle,
                    "state": "failed_before_checkpoint",
                }

        facade = Facade()
        authorize_payload: dict[str, object] = {
            "expected_cut": {
                "project_commit": 7,
                "current_root_digest": "a" * 64,
                "transition_head_digest": "b" * 64,
                "canonical_authority_digest": "c" * 64,
            }
        }
        bind_payload: dict[str, object] = {
            "executiveEpochId": epoch_handle,
            "rootThreadId": "thread.root",
            "workspaceRoot": "C:\\rh-mission\\workspace",
        }
        suspended_payload: dict[str, object] = {
            **bind_payload,
            "expected_cut": authorize_payload["expected_cut"],
        }
        semantic_request = _request("orient", {})
        semantic_binding: dict[str, object] = {
            "rootThreadId": "thread.root",
            "executiveEpochId": epoch_handle,
        }
        observation: dict[str, object] = {
            "observationId": "observation.1",
            "materialKind": "output",
            "content": "Exact investigator output.",
            "rootThreadId": "thread.root",
            "parentThreadId": "thread.root",
            "childThreadId": "thread.child.1",
        }
        epoch_binding: dict[str, object] = {
            "rootThreadId": "thread.root",
            "executiveEpochId": epoch_handle,
        }
        context: dict[str, object] = {
            "threadId": "thread.child.1",
            "reason": "goal_failed",
            "containmentScope": "goal_local",
        }
        fence_binding: dict[str, object] = {
            "executiveEpochId": epoch_handle,
        }
        fence_context = {
            **context,
            "reason": "unknown_effect",
            "containmentScope": "mission_fence",
        }
        failed_payload: dict[str, object] = {
            "executiveEpochId": epoch_handle,
            "reconciliation": {
                "stage": "goal_runtime",
                "failure_reason": "goal ended without a checkpoint",
            },
        }
        cases = [
            (
                "authorize_executive_epoch",
                authorize_payload,
                {
                    "executive_epoch_id": epoch_handle,
                    "state": "authorized",
                },
            ),
            (
                "validate_suspended_epoch_cut",
                suspended_payload,
                {
                    "executive_epoch_id": epoch_handle,
                    "root_thread_id": "thread.root",
                    "state": "bound",
                    "cut_validated": True,
                },
            ),
            (
                "bind_executive_epoch",
                bind_payload,
                {"executive_epoch_id": epoch_handle, "state": "bound"},
            ),
            (
                "execute_semantic_operation",
                {"request": semantic_request, "binding": semantic_binding},
                orient_result,
            ),
            (
                "reconstruct",
                {},
                {"state": "reconstructed"},
            ),
            (
                "capture_native_material_observation",
                {"observation": observation, "binding": epoch_binding},
                {"status": "captured"},
            ),
            (
                "fence_mission",
                {"context": fence_context, "binding": fence_binding},
                {"status": "fenced"},
            ),
            (
                "record_direct_failed_executive_epoch",
                failed_payload,
                {
                    "executive_epoch_id": epoch_handle,
                    "state": "failed_before_checkpoint",
                },
            ),
        ]
        for action, action_payload, expected in cases:
            with self.subTest(action=action):
                exit_code, result = self._run_bridge(
                    _bridge_request(action, action_payload), facade
                )
                self.assertEqual(exit_code, 0, result)
                self.assertEqual(result["data"], expected)

        self.assertEqual(
            calls,
            [
                ("authorize", authorize_payload),
                ("validate_suspended", suspended_payload),
                ("bind", bind_payload),
                (
                    "execute",
                    {
                        "request": semantic_request,
                        "executive_epoch_id": epoch_handle,
                    },
                ),
                ("reconstruct", None),
                (
                    "capture",
                    {
                        "observation": observation,
                        "executive_epoch_id": epoch_handle,
                    },
                ),
                (
                    "fence",
                    {
                        "context": fence_context,
                        "executive_epoch_id": epoch_handle,
                    },
                ),
                ("failed", failed_payload),
            ],
        )

    def test_host_bridge_issues_and_executes_exact_delegated_historical_reads(
        self,
    ) -> None:
        epoch, epoch_handle = _epoch_projection()
        grant_request = {
            "child_thread_id": "thread.history",
            "assignment_mode": "historical_opportunity_scout",
            "assignment": "Search prior branches for a missing compatible estimate.",
            "context": {"id": "context:history-search", "revision": 1},
            "source_families": ["branches", "evidence"],
            "raw_body_policy": "metadata_only",
        }
        grant_binding = {
            "rootThreadId": "thread.root",
            "executiveEpochId": epoch_handle,
            "childThreadId": "thread.history",
            "parentThreadId": "thread.root",
            "depth": 1,
        }
        grant = {
            "schema_version": "mathematical_research.historical_read_grant.v1",
            "grant_id": "a" * 64,
            "assignment_id": "b" * 64,
        }
        delegated_requests = (
            _request(
                "retrieve",
                {
                    "mode": "history_inventory",
                    "purpose": "Page the granted fixed-cut retained history.",
                    "page_size": 25,
                },
            ),
            _request(
                "retrieve",
                {
                    "mode": "history_search",
                    "purpose": "Search the granted fixed-cut retained history.",
                    "query": "retained bridge",
                    "fields": ["content"],
                },
            ),
            _request(
                "retrieve",
                {
                    "mode": "history_read",
                    "purpose": "Read one exact granted historical owner.",
                    "ids": ["branch:branch.history@1"],
                    "include_raw_bodies": False,
                },
            ),
            _request(
                "retrieve",
                {
                    "mode": "history_traverse",
                    "purpose": "Traverse one exact granted historical edge.",
                    "ids": ["branch:branch.history@1"],
                    "relationship_kinds": ["revision_predecessor"],
                },
            ),
        )
        delegated_binding = {
            "rootThreadId": "thread.root",
            "executiveEpochId": epoch_handle,
            "callerThreadId": "thread.history",
            "parentThreadId": "thread.root",
            "depth": 1,
            "turnId": "turn.history.1",
            "grantId": "a" * 64,
        }
        grant_owner_binding = {
            "project_id": "project.rh",
            "mission_id": "mission.1",
            "executive_epoch_id": epoch_handle,
            "root_thread_id": "thread.root",
            "actual_child_thread_id": "thread.history",
            "actual_parent_thread_id": "thread.root",
            "actual_depth": 1,
        }
        delegated_owner_binding = {
            **grant_owner_binding,
            "actual_child_thread_id": "thread.history",
            "expected_grant_id": "a" * 64,
        }
        delegated_result = {
            "schema_version": TOOL.MISSION_SEMANTIC_RESULT_SCHEMA_VERSION,
            "operation": "retrieve",
            "status": "completed",
            "result": {
                "view": "historical",
                "purpose": "Orient the bounded historical assignment.",
                "mode": "history_inventory",
                "project_commit_cut": 7,
                "source_families": ["branches", "evidence"],
                "raw_body_policy": "metadata_only",
                "coverage": {
                    "meaning": "All authorized retained items at the fixed cut.",
                    "omissions": [],
                    "limits": ["No raw capture bodies were authorized."],
                },
                "orientation": {
                    "schema_version": "mathematical_research.historical_orientation.v2",
                    "mission_purpose": "Advance the parent mathematical question.",
                    "proof_boundary": "Only exact independent reconstruction establishes proof.",
                    "context_id": "context:history-search",
                    "context_revision": 1,
                    "context_purpose": "Test one bounded historical opportunity.",
                    "question": "Which retained route now composes?",
                    "bottleneck_or_search_lens": (
                        "Search prior branches for a missing compatible estimate."
                    ),
                    "assignment_mode": "historical_opportunity_scout",
                    "assignment": (
                        "Search prior branches for a missing compatible estimate."
                    ),
                    "source_families": ["branches", "evidence"],
                    "indispensable_ground": [
                        {
                            "id": "branch:active-route",
                            "revision": 1,
                            "why": "Defines the current missing estimate.",
                        }
                    ],
                    "current_owner_references": [
                        {
                            "id": "branch:active-route",
                            "revision": 1,
                            "retrieval": "exact owner revision",
                            "provenance": "current Context ground",
                        }
                    ],
                    "known_omissions": [],
                    "restricted_uses": [],
                    "restrictions": [],
                    "invalidation_conditions": [],
                    "coverage_limits": ["No raw capture bodies were authorized."],
                    "assignment_ground": [
                        {
                            "id": "branch:active-route@1",
                            "kind": "branch",
                            "title": "Active route",
                            "readable_content": "Current route boundary.",
                            "completeness": "structured_view",
                        }
                    ],
                    "retrieval_routes": [
                        "history_inventory: page the authorized retained handles at the fixed cut"
                    ],
                },
                "items": [],
                "next_cursor": None,
            },
            "error": None,
        }
        calls: list[tuple[str, object]] = []

        class Facade:
            @staticmethod
            def current_epoch() -> dict[str, object]:
                return epoch

            @staticmethod
            def issue_historical_read_grant(
                request: object, *, binding: object
            ) -> dict[str, object]:
                calls.append(("grant", {"request": request, "binding": binding}))
                return grant

            @staticmethod
            def execute_delegated_read(
                request: object, *, grant: object, binding: object
            ) -> dict[str, object]:
                calls.append(
                    (
                        "read",
                        {"request": request, "grant": grant, "binding": binding},
                    )
                )
                response = json.loads(json.dumps(delegated_result))
                response["result"]["mode"] = request["input"]["mode"]
                return response

        exit_code, result = self._run_bridge(
            _bridge_request(
                "issue_historical_read_grant",
                {"request": grant_request, "binding": grant_binding},
            ),
            Facade(),
        )
        self.assertEqual(exit_code, 0, result)
        self.assertEqual(result["data"], grant)

        for delegated_request in delegated_requests:
            with self.subTest(mode=delegated_request["input"]["mode"]):
                self.assertEqual(
                    TOOL.deep_thaw(
                        TOOL.validate_historical_read_request(delegated_request)
                    ),
                    delegated_request,
                )
                with self.assertRaises(TOOL.MissionOperationContractError):
                    TOOL.validate_semantic_request(delegated_request)
                exit_code, result = self._run_bridge(
                    _bridge_request(
                        "execute_delegated_read",
                        {
                            "request": delegated_request,
                            "grant": grant,
                            "binding": delegated_binding,
                        },
                    ),
                    Facade(),
                )
                self.assertEqual(exit_code, 0, result)
                self.assertEqual(
                    result["data"]["result"]["mode"],
                    delegated_request["input"]["mode"],
                )
        self.assertEqual(
            calls,
            [
                (
                    "grant",
                    {"request": grant_request, "binding": grant_owner_binding},
                ),
                *(
                    (
                        "read",
                        {
                            "request": delegated_request,
                            "grant": grant,
                            "binding": delegated_owner_binding,
                        },
                    )
                    for delegated_request in delegated_requests
                ),
            ],
        )

        missing_grant_identity = json.loads(
            json.dumps(
                {
                    "request": delegated_requests[0],
                    "grant": grant,
                    "binding": delegated_binding,
                }
            )
        )
        del missing_grant_identity["binding"]["grantId"]
        exit_code, result = self._run_bridge(
            _bridge_request("execute_delegated_read", missing_grant_identity),
            Facade(),
        )
        self.assertEqual(exit_code, 1)
        self.assertEqual(
            result["errors"][0]["code"], "mission_host_bridge_request_invalid"
        )

        wrong_child = json.loads(
            json.dumps({"request": grant_request, "binding": grant_binding})
        )
        wrong_child["binding"]["childThreadId"] = "thread.sibling"
        exit_code, result = self._run_bridge(
            _bridge_request("issue_historical_read_grant", wrong_child),
            Facade(),
        )
        self.assertEqual(exit_code, 1)
        self.assertEqual(
            result["errors"][0]["code"], "mission_host_bridge_binding_mismatch"
        )

        write_request = _strategy_request()
        exit_code, result = self._run_bridge(
            _bridge_request(
                "execute_delegated_read",
                {
                    "request": write_request,
                    "grant": grant,
                    "binding": delegated_binding,
                },
            ),
            Facade(),
        )
        self.assertEqual(exit_code, 1)
        self.assertEqual(result["errors"][0]["code"], "mission_operation_not_allowed")

    def test_host_bridge_issues_and_executes_exact_candidate_a1_review(self) -> None:
        epoch, epoch_handle = _epoch_projection()
        grant_request = {
            "child_thread_id": "thread.a1-review",
            "assignment": "Independently reconstruct the frozen Candidate A1.",
            "context": {"id": "context:a1-review", "revision": 3},
            "candidate_ref": {
                "id": "candidate:purported-rh-proof",
                "revision": 2,
                "payload_sha256": "a" * 64,
            },
        }
        grant_binding = {
            "rootThreadId": "thread.root",
            "executiveEpochId": epoch_handle,
            "childThreadId": "thread.a1-review",
            "parentThreadId": "thread.root",
            "depth": 1,
        }
        grant = {
            "schema_version": "mathematical_research.candidate_a1_review_grant.v1",
            "grant_id": "b" * 64,
            "grant_digest_sha256": "b" * 64,
        }
        review_request = {
            "mode": "submit",
            "disposition": "admission_ready",
            "review_finding": "No remaining material objection was identified.",
            "no_remaining_material_objection": True,
        }
        review_binding = {
            "rootThreadId": "thread.root",
            "executiveEpochId": epoch_handle,
            "callerThreadId": "thread.a1-review",
            "parentThreadId": "thread.root",
            "depth": 1,
            "turnId": "turn.a1-review.1",
        }
        owner_binding = {
            "project_id": "project.rh",
            "mission_id": "mission.1",
            "executive_epoch_id": epoch_handle,
            "root_thread_id": "thread.root",
            "actual_child_thread_id": "thread.a1-review",
            "actual_parent_thread_id": "thread.root",
            "actual_depth": 1,
        }
        submission = {
            "schema_version": (
                "mathematical_research.candidate_a1_review_submission.v1"
            ),
            "status": "completed",
            "canonical_effect": "none",
            "public_effect": "none",
        }
        calls: list[tuple[str, object]] = []

        class Facade:
            @staticmethod
            def current_epoch() -> dict[str, object]:
                return epoch

            @staticmethod
            def issue_candidate_a1_review_grant(
                request: object, *, binding: object
            ) -> dict[str, object]:
                calls.append(("grant", {"request": request, "binding": binding}))
                return grant

            @staticmethod
            def execute_candidate_a1_review(
                request: object, *, grant: object, binding: object
            ) -> dict[str, object]:
                calls.append(
                    (
                        "review",
                        {"request": request, "grant": grant, "binding": binding},
                    )
                )
                return submission

        facade = Facade()
        exit_code, result = self._run_bridge(
            _bridge_request(
                "issue_candidate_a1_review_grant",
                {"request": grant_request, "binding": grant_binding},
            ),
            facade,
        )
        self.assertEqual(exit_code, 0, result)
        self.assertEqual(result["data"], grant)

        exit_code, result = self._run_bridge(
            _bridge_request(
                "execute_candidate_a1_review",
                {
                    "request": review_request,
                    "grant": grant,
                    "binding": review_binding,
                },
            ),
            facade,
        )
        self.assertEqual(exit_code, 0, result)
        self.assertEqual(result["data"], submission)
        self.assertEqual(
            calls,
            [
                ("grant", {"request": grant_request, "binding": owner_binding}),
                (
                    "review",
                    {
                        "request": review_request,
                        "grant": grant,
                        "binding": owner_binding,
                    },
                ),
            ],
        )

        self_asserting = json.loads(
            json.dumps(
                {
                    "request": {
                        **review_request,
                        "reviewer_thread_id": "thread.a1-review",
                    },
                    "grant": grant,
                    "binding": review_binding,
                }
            )
        )
        exit_code, result = self._run_bridge(
            _bridge_request("execute_candidate_a1_review", self_asserting),
            facade,
        )
        self.assertEqual(exit_code, 1)
        self.assertEqual(
            result["errors"][0]["code"], "candidate_a1_review_request_invalid"
        )

    def test_host_bridge_exposes_exact_role_scoped_complete_claim_admission(self) -> None:
        epoch, epoch_handle = _epoch_projection()
        case_request = {
            "candidate_ref": {
                "id": "candidate:purported-rh-proof",
                "revision": 2,
                "payload_sha256": "a" * 64,
            }
        }
        case_result = {
            "schema_version": (
                "mathematical_research.complete_claim_admission_case_result.v1"
            ),
            "status": "opened",
            "case_ref": {
                "id": "evidence:admission-case",
                "revision": 1,
                "payload_sha256": "b" * 64,
            },
            "canonical_effect": "none",
            "public_effect": "none",
        }
        grant_request = {
            "child_thread_id": "thread.admission-reviewer",
            "assignment": "Independently reconstruct the frozen Admission Case.",
            "context": {"id": "context:admission", "revision": 3},
            "case_ref": case_result["case_ref"],
        }
        grant_binding = {
            "rootThreadId": "thread.root",
            "executiveEpochId": epoch_handle,
            "childThreadId": "thread.admission-reviewer",
            "parentThreadId": "thread.root",
            "depth": 1,
        }
        review_grant = {
            "schema_version": (
                "mathematical_research.complete_claim_admission_grant.v1"
            ),
            "grant_id": "c" * 64,
            "role": "independent_complete_claim_admission_reviewer",
        }
        decision_grant_request = {
            **grant_request,
            "child_thread_id": "thread.admission-admitter",
            "assignment": "Make the role-disjoint exact Admission decision.",
        }
        decision_grant_binding = {
            **grant_binding,
            "childThreadId": "thread.admission-admitter",
        }
        decision_grant = {
            "schema_version": (
                "mathematical_research.complete_claim_admission_grant.v1"
            ),
            "grant_id": "d" * 64,
            "role": "independent_complete_claim_admitter",
        }
        review_request = {"mode": "retrieve"}
        review_binding = {
            "rootThreadId": "thread.root",
            "executiveEpochId": epoch_handle,
            "callerThreadId": "thread.admission-reviewer",
            "parentThreadId": "thread.root",
            "depth": 1,
            "turnId": "turn.admission-reviewer.1",
        }
        decision_request = {
            "mode": "submit",
            "disposition": "authorize_exact_delta",
            "decision_basis": "The frozen exact claim and independent review qualify.",
        }
        decision_binding = {
            **review_binding,
            "callerThreadId": "thread.admission-admitter",
            "turnId": "turn.admission-admitter.1",
        }
        calls: list[tuple[str, object]] = []

        class Facade:
            @staticmethod
            def current_epoch() -> dict[str, object]:
                return epoch

            @staticmethod
            def open_complete_claim_admission_case(
                request: object, *, executive_epoch_id: str
            ) -> dict[str, object]:
                calls.append(
                    (
                        "open",
                        {
                            "request": request,
                            "executive_epoch_id": executive_epoch_id,
                        },
                    )
                )
                return case_result

            @staticmethod
            def issue_admission_review_grant(
                request: object, *, binding: object
            ) -> dict[str, object]:
                calls.append(("review_grant", {"request": request, "binding": binding}))
                return review_grant

            @staticmethod
            def issue_admission_decision_grant(
                request: object, *, binding: object
            ) -> dict[str, object]:
                calls.append(("decision_grant", {"request": request, "binding": binding}))
                return decision_grant

            @staticmethod
            def execute_admission_review(
                request: object, *, grant: object, binding: object
            ) -> dict[str, object]:
                calls.append(
                    (
                        "review",
                        {"request": request, "grant": grant, "binding": binding},
                    )
                )
                return {"mode": "retrieve", "canonical_effect": "none"}

            @staticmethod
            def execute_admission_decision(
                request: object, *, grant: object, binding: object
            ) -> dict[str, object]:
                calls.append(
                    (
                        "decision",
                        {"request": request, "grant": grant, "binding": binding},
                    )
                )
                return {
                    "status": "completed",
                    "disposition": "authorize_exact_delta",
                    "canonical_effect": "none",
                    "public_effect": "none",
                }

        facade = Facade()
        for action, payload, expected in (
            (
                "open_complete_claim_admission_case",
                {
                    "request": case_request,
                    "binding": {
                        "rootThreadId": "thread.root",
                        "executiveEpochId": epoch_handle,
                    },
                },
                case_result,
            ),
            (
                "issue_admission_review_grant",
                {"request": grant_request, "binding": grant_binding},
                review_grant,
            ),
            (
                "issue_admission_decision_grant",
                {
                    "request": decision_grant_request,
                    "binding": decision_grant_binding,
                },
                decision_grant,
            ),
            (
                "execute_admission_review",
                {
                    "request": review_request,
                    "grant": review_grant,
                    "binding": review_binding,
                },
                {"mode": "retrieve", "canonical_effect": "none"},
            ),
            (
                "execute_admission_decision",
                {
                    "request": decision_request,
                    "grant": decision_grant,
                    "binding": decision_binding,
                },
                {
                    "status": "completed",
                    "disposition": "authorize_exact_delta",
                    "canonical_effect": "none",
                    "public_effect": "none",
                },
            ),
        ):
            exit_code, result = self._run_bridge(
                _bridge_request(action, payload), facade
            )
            self.assertEqual(exit_code, 0, result)
            self.assertEqual(result["data"], expected)

        reviewer_owner_binding = {
            "project_id": "project.rh",
            "mission_id": "mission.1",
            "executive_epoch_id": epoch_handle,
            "root_thread_id": "thread.root",
            "actual_child_thread_id": "thread.admission-reviewer",
            "actual_parent_thread_id": "thread.root",
            "actual_depth": 1,
        }
        admitter_owner_binding = {
            **reviewer_owner_binding,
            "actual_child_thread_id": "thread.admission-admitter",
        }
        self.assertEqual(
            calls,
            [
                (
                    "open",
                    {"request": case_request, "executive_epoch_id": epoch_handle},
                ),
                (
                    "review_grant",
                    {"request": grant_request, "binding": reviewer_owner_binding},
                ),
                (
                    "decision_grant",
                    {
                        "request": decision_grant_request,
                        "binding": admitter_owner_binding,
                    },
                ),
                (
                    "review",
                    {
                        "request": review_request,
                        "grant": review_grant,
                        "binding": reviewer_owner_binding,
                    },
                ),
                (
                    "decision",
                    {
                        "request": decision_request,
                        "grant": decision_grant,
                        "binding": admitter_owner_binding,
                    },
                ),
            ],
        )

        wrong_child = json.loads(
            json.dumps(
                {
                    "request": decision_request,
                    "grant": decision_grant,
                    "binding": {
                        **decision_binding,
                        "parentThreadId": "thread.unrelated-parent",
                    },
                }
            )
        )
        exit_code, result = self._run_bridge(
            _bridge_request("execute_admission_decision", wrong_child), facade
        )
        self.assertEqual(exit_code, 1)
        self.assertEqual(
            result["errors"][0]["code"], "mission_host_bridge_binding_mismatch"
        )
        self.assertEqual(len(calls), 5)

        self_asserting_role = json.loads(
            json.dumps(
                {
                    "request": {**review_request, "role": "admitter"},
                    "grant": review_grant,
                    "binding": review_binding,
                }
            )
        )
        exit_code, result = self._run_bridge(
            _bridge_request("execute_admission_review", self_asserting_role), facade
        )
        self.assertEqual(exit_code, 1)
        self.assertEqual(
            result["errors"][0]["code"], "admission_review_request_invalid"
        )
        self.assertEqual(len(calls), 5)

    def test_formal_host_action_keeps_runtime_facts_server_owned_and_maps_control(
        self,
    ) -> None:
        epoch, epoch_handle = _epoch_projection()
        authority = object()
        store = object()
        cas = object()
        lease = object()
        adapter = object()
        factual_result = {
            "schema": "mr.formal_attempt_reconciliation.v1",
            "session_id": "session.formal.1",
            "attempt_id": "attempt.formal.1",
            "attempt_state": "cancelled",
            "provider_effect_certainty": "known",
            "result_digest_sha256": "b" * 64,
            "raw_capture": None,
            "session_was_terminal": False,
            "session_is_terminal": True,
            "result_bound_to_session": True,
            "terminal_transition_replayed": False,
        }

        class Facade:
            _store = store
            _cas = cas

            @staticmethod
            def current_epoch() -> dict[str, object]:
                return epoch

            @staticmethod
            def _direct_epoch_authority(value: str) -> object:
                self.assertEqual(value, epoch_handle)
                return authority

            @staticmethod
            def _writer_lease() -> object:
                return lease

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            release_root = root / "release"
            codex_home = root / "codex-home"
            release_root.mkdir()
            codex_home.mkdir()
            runtime_payload = {
                "release_root": str(release_root),
                "release_commit": "a" * 40,
                "state_root": str(root / "formal-state"),
                "codex_executable": str(Path(sys.executable).resolve()),
                "codex_home": str(codex_home),
                "outer_containment_id": "rh-host-boundary.1",
                "trusted_mcp_server_ids": ["mcp.readonly"],
                "trusted_app_ids": ["app.readonly"],
                "polling_cadence_seconds": 0.125,
            }
            payload = {
                "request": {
                    "selected_bet_sha256": "c" * 64,
                    "correction_basis": None,
                },
                "binding": {
                    "rootThreadId": "thread.root",
                    "executiveEpochId": epoch_handle,
                },
                "runtime": runtime_payload,
            }
            for requested, force_signal in (("cancel", None), ("force_stop", 99)):
                with self.subTest(requested=requested):
                    installed: dict[int, object] = {}

                    def install(signum: int, handler: object) -> None:
                        installed[int(signum)] = handler

                    def execute(*args: object, **kwargs: object) -> object:
                        self.assertEqual(args, (store,))
                        control = kwargs["control"]
                        signum = (
                            int(TOOL.signal.SIGTERM)
                            if force_signal is None
                            else force_signal
                        )
                        handler = installed[signum]
                        assert callable(handler)
                        handler(signum, None)
                        self.assertEqual(control.requested, requested)
                        runtime = kwargs["runtime"]
                        self.assertEqual(runtime.release_root, release_root)
                        self.assertEqual(runtime.state_root, root / "formal-state")
                        self.assertEqual(runtime.trusted_mcp_server_ids, ("mcp.readonly",))
                        self.assertEqual(runtime.trusted_app_ids, ("app.readonly",))
                        self.assertEqual(kwargs["authority"], authority)
                        self.assertEqual(kwargs["adapter"], adapter)
                        self.assertEqual(kwargs["lease"], lease)
                        self.assertEqual(kwargs["selected_bet_sha256"], "c" * 64)
                        self.assertIsNone(kwargs["correction_basis"])
                        return SimpleNamespace(to_payload=lambda: factual_result)

                    signal_patch = (
                        patch.object(TOOL.signal, "SIGUSR2", force_signal, create=True)
                        if force_signal is not None
                        else patch.object(TOOL.signal, "SIGUSR2", None, create=True)
                    )
                    with (
                        patch.object(
                            TOOL,
                            "build_formal_attempt_adapter",
                            return_value=adapter,
                        ) as built,
                        patch.object(
                            TOOL,
                            "execute_formal_attempt_operation",
                            side_effect=execute,
                        ),
                        patch.object(TOOL.signal, "getsignal", return_value="prior"),
                        patch.object(TOOL.signal, "signal", side_effect=install),
                        signal_patch,
                    ):
                        exit_code, result = self._run_bridge(
                            _bridge_request("execute_formal_attempt", payload),
                            Facade(),
                        )
                    self.assertEqual(exit_code, 0, result)
                    self.assertEqual(result["data"], factual_result)
                    built.assert_called_once()

            model_injected = json.loads(json.dumps(payload))
            model_injected["request"]["provider_id"] = "model-selected-provider"
            exit_code, result = self._run_bridge(
                _bridge_request("execute_formal_attempt", model_injected),
                Facade(),
            )
            self.assertEqual(exit_code, 1)
            self.assertEqual(
                result["errors"][0]["code"],
                "mission_host_bridge_request_invalid",
            )

    def test_host_bridge_transports_large_semantic_input_and_owner_result(self) -> None:
        epoch, epoch_handle = _epoch_projection()
        large_text = "mathematical-content:" + ("x" * (5 * 1024 * 1024))
        captured: list[object] = []

        class Facade:
            @staticmethod
            def current_epoch() -> dict[str, object]:
                return epoch

            @staticmethod
            def capture_native_material_observation(
                value: object, *, executive_epoch_id: str
            ) -> dict[str, object]:
                self.assertEqual(executive_epoch_id, epoch_handle)
                captured.append(value)
                return {"status": "captured"}

            @staticmethod
            def reconstruct() -> dict[str, object]:
                return {"ordinary_large_read": large_text, "canonical_effect": "none"}

        observation = {
            "observationId": "observation.large",
            "materialKind": "output",
            "content": large_text,
            "rootThreadId": "thread.root",
            "parentThreadId": "thread.root",
            "childThreadId": "thread.child.large",
        }
        binding = {
            "rootThreadId": "thread.root",
            "executiveEpochId": epoch_handle,
        }
        exit_code, result = self._run_bridge(
            _bridge_request(
                "capture_native_material_observation",
                {"observation": observation, "binding": binding},
            ),
            Facade(),
        )
        self.assertEqual(exit_code, 0, result)
        self.assertEqual(captured, [observation])

        exit_code, result = self._run_bridge(
            _bridge_request("reconstruct", {}),
            Facade(),
        )
        self.assertEqual(exit_code, 0, result)
        self.assertEqual(result["data"]["ordinary_large_read"], large_text)

    def test_native_capture_uses_historical_epoch_binding_not_current_authority(
        self,
    ) -> None:
        observation = {
            "observationId": "observation.late",
            "materialKind": "output",
            "content": "Late immutable material from a completed predecessor.",
            "rootThreadId": "thread.predecessor",
            "parentThreadId": "thread.predecessor",
            "childThreadId": "thread.child.late",
        }
        captured: list[object] = []

        def capture(
            value: object, *, executive_epoch_id: str
        ) -> dict[str, object]:
            captured.append(
                {
                    "observation": value,
                    "executive_epoch_id": executive_epoch_id,
                }
            )
            return {"status": "captured", "canonical_effect": "none"}

        facade = SimpleNamespace(
            current_epoch=lambda: (_ for _ in ()).throw(
                AssertionError("historical capture must not query current authority")
            ),
            capture_native_material_observation=capture,
        )
        binding = {
            "rootThreadId": "thread.predecessor",
            "executiveEpochId": "epoch.predecessor",
        }
        exit_code, result = self._run_bridge(
            _bridge_request(
                "capture_native_material_observation",
                {"observation": observation, "binding": binding},
            ),
            facade,
        )
        self.assertEqual(exit_code, 0, result)
        self.assertEqual(
            captured,
            [
                {
                    "observation": observation,
                    "executive_epoch_id": "epoch.predecessor",
                }
            ],
        )

    def test_host_bridge_surfaces_sqlite_busy_without_automatic_replay(self) -> None:
        request = _bridge_request("reconstruct", {})
        for error_code in (
            sqlite3.SQLITE_BUSY,
            sqlite3.SQLITE_BUSY_RECOVERY,
            sqlite3.SQLITE_BUSY_SNAPSHOT,
        ):
            with self.subTest(error_code=error_code):
                busy = sqlite3.OperationalError("database is busy")
                busy.sqlite_errorcode = error_code
                busy.sqlite_errorname = "SQLITE_BUSY_EXTENDED"
                with tempfile.TemporaryDirectory() as temporary:
                    path = Path(temporary) / "bridge.json"
                    path.write_text(json.dumps(request), encoding="utf-8")
                    arguments = SimpleNamespace(request=str(path.resolve()))
                    options = {
                        "workspace_root": "C:\\rh-mission\\workspace",
                        "project_id": "project.rh",
                        "mission_id": "mission.1",
                    }
                    with (
                        patch.object(
                            TOOL.MissionInterface,
                            "open",
                            side_effect=busy,
                        ) as opened,
                        self.assertRaises(TOOL.MissionInterfaceError) as raised,
                    ):
                        TOOL._run_host_bridge(arguments, options)
                self.assertEqual(raised.exception.code, "mission_operation_unavailable")
                self.assertIn("no automatic replay occurred", str(raised.exception))
                opened.assert_called_once_with(
                    "C:\\rh-mission\\workspace", "project.rh", "mission.1"
                )

    def test_host_bridge_does_not_retry_sqlite_locked(self) -> None:
        request = _bridge_request("reconstruct", {})
        locked = sqlite3.OperationalError("database table is locked")
        locked.sqlite_errorcode = sqlite3.SQLITE_LOCKED
        locked.sqlite_errorname = "SQLITE_LOCKED"
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "bridge.json"
            path.write_text(json.dumps(request), encoding="utf-8")
            arguments = SimpleNamespace(request=str(path.resolve()))
            options = {
                "workspace_root": "C:\\rh-mission\\workspace",
                "project_id": "project.rh",
                "mission_id": "mission.1",
            }
            with (
                patch.object(TOOL.MissionInterface, "open", side_effect=locked) as opened,
                self.assertRaises(sqlite3.OperationalError) as raised,
            ):
                TOOL._run_host_bridge(arguments, options)
        self.assertIs(raised.exception, locked)
        opened.assert_called_once_with(
            "C:\\rh-mission\\workspace", "project.rh", "mission.1"
        )

    def test_host_bridge_rejects_shape_identity_and_epoch_mismatches(self) -> None:
        epoch, epoch_handle = _epoch_projection()
        facade = SimpleNamespace(
            current_epoch=lambda: epoch,
            execute_semantic_operation=lambda _request, *, executive_epoch_id: {},
        )
        valid_payload = {
            "request": _request("orient", {}),
            "binding": {
                "rootThreadId": "thread.root",
                "executiveEpochId": epoch_handle,
            },
        }
        mismatched_root_payload = json.loads(json.dumps(valid_payload))
        mismatched_root_payload["binding"]["rootThreadId"] = "thread.other"
        mismatched_root = _bridge_request(
            "execute_semantic_operation", mismatched_root_payload
        )
        exit_code, result = self._run_bridge(mismatched_root, facade)
        self.assertEqual(exit_code, 1)
        self.assertEqual(
            result["errors"][0]["code"], "mission_host_bridge_binding_mismatch"
        )

        mismatched_epoch_payload = json.loads(json.dumps(valid_payload))
        mismatched_epoch_payload["binding"]["executiveEpochId"] = "epoch.other"
        mismatched_epoch = _bridge_request(
            "execute_semantic_operation", mismatched_epoch_payload
        )
        exit_code, result = self._run_bridge(mismatched_epoch, facade)
        self.assertEqual(exit_code, 1)
        self.assertEqual(
            result["errors"][0]["code"], "mission_host_bridge_binding_mismatch"
        )

        missing_root_payload = json.loads(json.dumps(valid_payload))
        missing_root_payload["binding"].pop("rootThreadId")
        missing_root = _bridge_request(
            "execute_semantic_operation", missing_root_payload
        )
        exit_code, result = self._run_bridge(missing_root, facade)
        self.assertEqual(exit_code, 1)
        self.assertEqual(
            result["errors"][0]["code"], "mission_host_bridge_request_invalid"
        )

        missing_epoch_payload = json.loads(json.dumps(valid_payload))
        missing_epoch_payload["binding"].pop("executiveEpochId")
        missing_epoch = _bridge_request(
            "execute_semantic_operation", missing_epoch_payload
        )
        exit_code, result = self._run_bridge(missing_epoch, facade)
        self.assertEqual(exit_code, 1)
        self.assertEqual(
            result["errors"][0]["code"], "mission_host_bridge_request_invalid"
        )

        obsolete_role_payload = json.loads(json.dumps(valid_payload))
        obsolete_role_payload["binding"]["role"] = "predecessor"
        obsolete_role = _bridge_request(
            "execute_semantic_operation", obsolete_role_payload
        )
        exit_code, result = self._run_bridge(obsolete_role, facade)
        self.assertEqual(exit_code, 1)
        self.assertEqual(
            result["errors"][0]["code"], "mission_host_bridge_request_invalid"
        )

        extra_key = _bridge_request("execute_semantic_operation", valid_payload)
        extra_key["unexpected"] = True
        exit_code, result = self._run_bridge(extra_key, facade)
        self.assertEqual(exit_code, 1)
        self.assertEqual(
            result["errors"][0]["code"], "mission_host_bridge_request_invalid"
        )

        wrong_selector = _bridge_request(
            "execute_semantic_operation", valid_payload, project_id="project.other"
        )
        exit_code, result = self._run_bridge(wrong_selector, facade)
        self.assertEqual(exit_code, 1)
        self.assertEqual(
            result["errors"][0]["code"], "mission_host_bridge_binding_mismatch"
        )

        old_storage_action = _bridge_request("execute_storage_operation", {})
        exit_code, result = self._run_bridge(old_storage_action, facade)
        self.assertEqual(exit_code, 1)
        self.assertEqual(
            result["errors"][0]["code"], "mission_host_bridge_request_invalid"
        )

        obsolete_reconstruct_role = _bridge_request(
            "reconstruct",
            {
                "role": "host",
            },
        )
        exit_code, result = self._run_bridge(obsolete_reconstruct_role, facade)
        self.assertEqual(exit_code, 1)
        self.assertEqual(
            result["errors"][0]["code"], "mission_host_bridge_request_invalid"
        )

    def test_host_bridge_propagates_owner_observation_rejection(self) -> None:
        epoch, epoch_handle = _epoch_projection()
        observation = {
            "observationId": "observation.1",
            "materialKind": "not-an-owner-kind",
            "content": "Exact observed material.",
            "rootThreadId": "thread.root",
            "parentThreadId": "thread.root",
            "childThreadId": "thread.child.1",
        }

        def reject_owner(
            _observation: object, *, executive_epoch_id: str
        ) -> dict[str, object]:
            self.assertEqual(executive_epoch_id, epoch_handle)
            raise TOOL.MissionInterfaceError(
                "mission_host_bridge_request_invalid",
                "native observation has an unsupported owner material kind.",
            )

        facade = SimpleNamespace(
            current_epoch=lambda: epoch,
            capture_native_material_observation=reject_owner,
        )
        request = _bridge_request(
            "capture_native_material_observation",
            {
                "observation": observation,
                "binding": {
                    "rootThreadId": "thread.root",
                    "executiveEpochId": epoch_handle,
                },
            },
        )
        exit_code, result = self._run_bridge(request, facade)
        self.assertEqual(exit_code, 1)
        self.assertEqual(
            result["errors"][0]["code"], "mission_host_bridge_request_invalid"
        )
        self.assertIn("owner material kind", result["errors"][0]["message"])

    def test_host_bridge_distinguishes_confirmed_shared_capture_integrity_loss(
        self,
    ) -> None:
        epoch, epoch_handle = _epoch_projection()
        observation = {
            "observationId": "observation.shared-integrity",
            "materialKind": "output",
            "content": "Exact observed material.",
            "rootThreadId": "thread.root",
            "parentThreadId": "thread.root",
            "childThreadId": "thread.child.shared-integrity",
        }
        request = _bridge_request(
            "capture_native_material_observation",
            {
                "observation": observation,
                "binding": {
                    "rootThreadId": "thread.root",
                    "executiveEpochId": epoch_handle,
                },
            },
        )

        for failure in (
            TOOL.WorkspaceIntegrityError("shared Store integrity changed"),
            TOOL.IntegrityFreeze("blob_conflict", "shared CAS bytes conflict"),
        ):
            with self.subTest(failure=type(failure).__name__):
                def reject_integrity(
                    _observation: object,
                    *,
                    executive_epoch_id: str,
                    captured_failure: Exception = failure,
                ) -> dict[str, object]:
                    self.assertEqual(executive_epoch_id, epoch_handle)
                    raise captured_failure

                facade = SimpleNamespace(
                    current_epoch=lambda: epoch,
                    capture_native_material_observation=reject_integrity,
                )
                exit_code, result = self._run_bridge(request, facade)
                self.assertEqual(exit_code, 1)
                self.assertEqual(
                    result["errors"][0]["code"],
                    "mission_shared_integrity_failure",
                )

    def test_host_bridge_dispatches_callable_without_capability_self_description(
        self,
    ) -> None:
        request = _bridge_request(
            "reconstruct",
            {},
        )
        self.assertFalse(hasattr(TOOL.MissionInterface, "capabilities"))
        exit_code, result = self._run_bridge(
            request,
            SimpleNamespace(reconstruct=lambda: {"state": "reconstructed"}),
        )
        self.assertEqual(exit_code, 0, result)
        self.assertEqual(result["data"], {"state": "reconstructed"})

    def test_host_bridge_reconstructs_exact_selected_readback_handles(self) -> None:
        handles = [
            "strategy:strategy.rh.mission-wide.6@1",
            "capture-artifact:capture.output.1:0",
        ]
        observed: list[list[str]] = []

        def reconstruct(*, selected_readback_handles: list[str]) -> dict[str, object]:
            observed.append(selected_readback_handles)
            return {"selected_readback": selected_readback_handles}

        exit_code, result = self._run_bridge(
            _bridge_request(
                "reconstruct",
                {"selected_readback_handles": handles},
            ),
            SimpleNamespace(reconstruct=reconstruct),
        )
        self.assertEqual(exit_code, 0, result)
        self.assertEqual(observed, [handles])
        self.assertEqual(result["data"]["selected_readback"], handles)

    def test_private_root_query_context_is_forwarded_separately_and_rejected_closed(self) -> None:
        epoch, epoch_id = _epoch_projection()
        key = "ab" * 32
        calls = []
        def execute(request, **options):
            calls.append((request, options))
            return _orientation_result()
        facade = SimpleNamespace(current_epoch=lambda: epoch, execute_semantic_operation=execute)
        payload = {"request": _request("orient", {}), "binding": {"rootThreadId": "thread.root", "executiveEpochId": epoch_id}, "root_query_context": {"cursor_mac_key": key}}
        exit_code, result = self._run_bridge(_bridge_request("execute_semantic_operation", payload), facade)
        self.assertEqual(exit_code, 0, result)
        self.assertEqual(calls, [(payload["request"], {"executive_epoch_id": epoch_id, "root_query_context": {"cursor_mac_key": key}})])
        self.assertNotIn(key, json.dumps(result))
        self.assertNotIn("root_query_context", payload["request"])
        for context in ({"cursor_mac_key": key, "root_thread_id": "other"}, {"cursor_mac_key": "not-a-key"}, {"signing_key": key}):
            with self.subTest(context_keys=tuple(context)):
                exit_code, rejected = self._run_bridge(_bridge_request("execute_semantic_operation", {**payload, "root_query_context": context}), facade)
                self.assertEqual(exit_code, 1)
                self.assertEqual(rejected["errors"][0]["code"], "mission_host_bridge_request_invalid")
                self.assertNotIn(key, json.dumps(rejected))
        self.assertEqual(len(calls), 1)
        model_injection = {**payload, "request": _request("orient", {"root_query_context": {"cursor_mac_key": key}})}
        exit_code, rejected = self._run_bridge(_bridge_request("execute_semantic_operation", model_injection), facade)
        self.assertEqual(exit_code, 1)
        self.assertNotIn(key, json.dumps(rejected))
        self.assertEqual(len(calls), 1)

    def test_host_snapshot_canonical_source_is_read_only_private_open_dependency(self) -> None:
        calls = []
        source = {"repo_root": str(REPO_ROOT.parent / ("b" * 40)), "release_sha": "b" * 40}
        snapshot = {
            "schema_version": "mathematical_research.mission_host_snapshot.v4",
            "authorization_cut": {
                "project_commit": 7,
                "current_root_digest": "a" * 64,
                "transition_head_digest": "b" * 64,
                "canonical_authority_digest": "c" * 64,
            },
            "current_state": {"private": "unchanged"},
            "executive_orientation": _orientation_result()["result"]["orientation"],
        }
        facade = SimpleNamespace(host_snapshot=lambda: calls.append("snapshot") or snapshot)
        with patch.object(TOOL, "load_canonical_snapshot") as loaded, patch.object(TOOL.MissionInterface, "open", return_value=facade) as opened:
            result = TOOL._dispatch_host_bridge_attempt(workspace_root="C:\\rh-mission\\workspace", project_id="project.rh", mission_id="mission.1", action="host_snapshot", payload={"canonical_source": source})
        self.assertEqual(result, snapshot)
        loaded.assert_not_called()
        opened.assert_called_once_with("C:\\rh-mission\\workspace", "project.rh", "mission.1", canonical_repo_root=source["repo_root"])
        self.assertEqual(calls, ["snapshot"])
        for invalid in (
            {"repo_root": "relative", "release_sha": "b" * 40},
            {**source, "effect": "write"},
            {**source, "source_commit": "a" * 40},
            {**source, "authority_vector": {}},
            {**source, "state_path": "research_state.json"},
            {**source, "release_sha": "main"},
            {**source, "release_sha": "c" * 40},
            {"repo_root": source["repo_root"], "source_commit": "b" * 40},
        ):
            with patch.object(TOOL, "load_canonical_snapshot") as not_loaded:
                exit_code, rejected = self._run_bridge(_bridge_request("host_snapshot", {"canonical_source": invalid}), facade)
                self.assertEqual(exit_code, 1)
                self.assertEqual(rejected["errors"][0]["code"], "mission_host_bridge_request_invalid")
                not_loaded.assert_not_called()
        exit_code, rejected = self._run_bridge(_bridge_request("reconstruct", {"canonical_source": source}), facade)
        self.assertEqual(exit_code, 1)
        self.assertEqual(calls, ["snapshot"])

    def test_private_mission_observation_dispatches_without_executive_or_writer_authority(self) -> None:
        key = "d7" * 32
        source = {
            "repo_root": str((REPO_ROOT.parent / ("b" * 40)).resolve()),
            "release_sha": "b" * 40,
        }
        binding = {
            "project_id": "project.rh", "mission_id": "mission.1",
            "root_identity": "synthetic-observation-root", "project_commit": 7,
            "root_digest": "a" * 64, "transition_head_digest": "b" * 64,
            "canonical_authority_digest": "c" * 64,
        }
        requests = (
            ({"operation": "current_decision"}, {"operation": "current_decision"}),
            ({"operation": "decision_history", "collection": "strategies"},
             {"operation": "decision_history", "collection": "strategies", "page_size": 25}),
            ({"operation": "exact_record", "handle": "evidence:fixture@1", "source_binding": binding},
             {"operation": "exact_record", "handle": "evidence:fixture@1", "source_binding": binding,
              "offset_bytes": 0, "max_bytes": 65536}),
        )
        forbidden = Mock(side_effect=AssertionError("observation acquired executive/writer authority"))
        for request, normalized in requests:
            with self.subTest(operation=request["operation"]):
                # This adapter fixture proves transport only. Core tests own the
                # semantic content and same-cut source/revision assertions.
                owner_result = {
                    "schema_version": "mathematical_research.mission_observation.v1",
                    "operation": request["operation"], "source_binding": binding,
                    "state": "empty", "reason": None, "completeness": "exhausted",
                    "items": [], "next_cursor": None, "coverage": None,
                }
                observed = Mock(return_value=owner_result)
                facade = SimpleNamespace(
                    observe_mission=observed, current_epoch=forbidden,
                    _direct_epoch_authority=forbidden, _writer_lease=forbidden,
                    execute_semantic_operation=forbidden,
                )
                payload = {"request": request, "cursor_mac_key": key, "canonical_source": source}
                action, parsed = TOOL._parse_host_bridge_request(
                    _bridge_request("observe_mission", payload),
                    project_id="project.rh", mission_id="mission.1",
                )
                with patch.object(TOOL, "load_canonical_snapshot") as loaded, patch.object(
                    TOOL.MissionInterface, "open", return_value=facade,
                ) as opened, patch.object(
                    TOOL, "_validate_current_epoch_causality", side_effect=forbidden,
                ):
                    result = TOOL._dispatch_host_bridge_attempt(
                        workspace_root="C:\\rh-mission\\workspace", project_id="project.rh",
                        mission_id="mission.1", action=action, payload=parsed,
                    )
                opened.assert_called_once_with(
                    "C:\\rh-mission\\workspace", "project.rh", "mission.1",
                    canonical_repo_root=source["repo_root"],
                )
                loaded.assert_not_called()
                observed.assert_called_once_with(normalized, cursor_mac_key=key)
                self.assertEqual(result, owner_result)
                self.assertNotIn(key, json.dumps(result))
                self.assertNotIn("canonical_source", result)
                self.assertNotIn("cursor_mac_key", parsed["request"])

        # Preserve the private response-file route, including a present empty
        # observation result; no JSON/key is written to ordinary stdout.
        observed = Mock(return_value=owner_result)
        exit_code, transported = self._run_bridge(
            _bridge_request("observe_mission", {"request": request, "cursor_mac_key": key}),
            SimpleNamespace(observe_mission=observed, current_epoch=forbidden, _writer_lease=forbidden),
        )
        self.assertEqual(exit_code, 0, transported)
        self.assertEqual(transported["data"], owner_result)
        observed.assert_called_once_with(normalized, cursor_mac_key=key)
        self.assertNotIn(key, json.dumps(transported))
        forbidden.assert_not_called()

    def test_private_mission_observation_rejects_invalid_input_before_workspace_open(self) -> None:
        key = "d7" * 32
        source = {
            "repo_root": str((REPO_ROOT.parent / ("b" * 40)).resolve()),
            "release_sha": "b" * 40,
        }
        payload = {"request": {"operation": "current_decision"}, "cursor_mac_key": key}
        invalid_payloads = (
            {"request": payload["request"]},
            {**payload, "cursor_mac_key": "invalid-private-key"},
            {**payload, "cursor_mac_key": "D7" * 32},
            {**payload, "cursor_mac_key": None},
            {**payload, "binding": {"executiveEpochId": "epoch.1", "rootThreadId": "thread.root"}},
            {**payload, "request": {"operation": "record_strategy", "private_input": key}},
            {**payload, "request": {"operation": "current_decision", "cursor_mac_key": key}},
            {**payload, "request": {"operation": "decision_history", "collection": "strategies", "page_size": 51}},
            {**payload, "request": {"operation": "decision_history", "collection": "strategies", "cursor": "unbound"}},
            {**payload, "request": {"operation": "exact_record", "handle": "evidence:fixture@1"}},
            {**payload, "request": {"operation": "current_decision", "source_binding": {"root_identity": key}}},
            {**payload, "canonical_source": {**source, "repo_root": "relative"}},
            {**payload, "canonical_source": {**source, "release_sha": "main"}},
            {**payload, "canonical_source": {**source, "release_sha": "c" * 40}},
            {**payload, "canonical_source": {**source, "source_commit": "a" * 40}},
            {**payload, "canonical_source": {**source, "state_path": "research_state.json"}},
        )
        with tempfile.TemporaryDirectory() as temporary:
            request_path = Path(temporary) / "request.json"
            for index, invalid in enumerate(invalid_payloads):
                with self.subTest(case=index):
                    response_path = Path(temporary) / f"response-{index}.json"
                    request_path.write_text(
                        json.dumps(_bridge_request("observe_mission", invalid)), encoding="utf-8",
                    )
                    stream = io.StringIO()
                    with patch.object(TOOL.MissionInterface, "open") as opened, patch.object(
                        TOOL, "load_canonical_snapshot",
                    ) as loaded, redirect_stdout(stream):
                        exit_code = TOOL.main([
                            "host-bridge", "--workspace-root", "C:\\rh-mission\\workspace",
                            "--project-id", "project.rh", "--mission-id", "mission.1",
                            "--request", str(request_path.resolve()), "--response", str(response_path.resolve()),
                            "--format", "json",
                        ])
                    self.assertEqual(exit_code, 1)
                    opened.assert_not_called()
                    loaded.assert_not_called()
                    self.assertEqual(stream.getvalue(), "")
                    rejected = json.loads(response_path.read_text(encoding="utf-8"))
                    self.assertEqual(rejected["errors"][0]["code"], "mission_host_bridge_request_invalid")
                    self.assertNotIn(key, json.dumps(rejected))
                    self.assertNotIn("invalid-private-key", json.dumps(rejected))

    def test_root_cursor_survives_two_real_bridge_subprocesses_and_capture_write(self) -> None:
        import test_mission_interface_direct as fixtures
        from research_core.mission_operation_contract import validate_semantic_result, validate_mission_host_snapshot
        from research_core.research_model import deep_thaw
        fixture = fixtures.DirectMissionInterfaceTests()
        fixture.setUpClass()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        epoch_id = fixture._authorize()
        fixture._bind(epoch_id)
        key = "bc" * 32
        binding = {"rootThreadId": fixture.goal_thread_id, "executiveEpochId": epoch_id}
        def run(action, payload):
            with tempfile.TemporaryDirectory() as temporary:
                request = Path(temporary) / "request.json"
                response = Path(temporary) / "response.json"
                request.write_text(json.dumps(_bridge_request(action, payload)), encoding="utf-8")
                before = deep_thaw(fixture.store.read_metadata())
                executed = self._run("host-bridge", "--workspace-root", str(fixture.store.paths.root), "--project-id", "project.rh", "--mission-id", "mission.1", "--request", str(request), "--response", str(response), "--format", "json")
                self.assertEqual(executed.returncode, 0, executed.stderr)
                self.assertEqual(executed.stdout, "")
                value = json.loads(response.read_text(encoding="utf-8"))
                self.assertNotIn(key, json.dumps(value) + executed.stderr)
                self.assertEqual(before, deep_thaw(fixture.store.read_metadata()))
                return value["data"]
        query = {"mode": "inventory", "purpose": "Select exact current owners.", "page_size": 1}
        first = run("execute_semantic_operation", {"request": _request("retrieve", query), "binding": binding, "root_query_context": {"cursor_mac_key": key}})
        validate_semantic_result("retrieve", first)
        self.assertIsNotNone(first["result"]["next_cursor"])
        fixture._capture_pending_material(epoch_id)
        second = run("execute_semantic_operation", {"request": _request("retrieve", {**query, "cursor": first["result"]["next_cursor"]}), "binding": binding, "root_query_context": {"cursor_mac_key": key}})
        validate_semantic_result("retrieve", second)
        self.assertNotEqual(first["result"]["items"][0]["id"], second["result"]["items"][0]["id"])
        selected_sha = "b" * 40
        selected_root = (Path(fixture.temporary.name) / "releases" / selected_sha).resolve()
        selected_bytes = selected_root / "projects/riemann_hypothesis/research_state.json"
        selected_bytes.parent.mkdir(parents=True)
        selected_bytes.write_bytes(fixture.canonical.authorized_path.read_bytes())
        snapshot = run("host_snapshot", {"canonical_source": {"repo_root": str(selected_root), "release_sha": selected_sha}})
        validate_mission_host_snapshot(snapshot)
        self.assertEqual(
            snapshot["current_state"]["schema_version"],
            "mathematical_research.mission_host_current_state.v2",
        )
        self.assertEqual(
            snapshot["current_state"]["latest_executive_epoch"],
            deep_thaw(fixture.interface.reconstruct())["latest_executive_epoch"],
        )
        self.assertEqual(snapshot["executive_orientation"], deep_thaw(fixture.interface.executive_orientation()))
        self.assertEqual(snapshot["current_state"]["canonical_authority"]["source_commit"], "a" * 40)
        self.assertNotIn("reconstruction", snapshot)
        self.assertNotIn("host_recovery_facts", snapshot)

    def test_host_bridge_requires_the_mapped_owner_method_to_be_callable(self) -> None:
        request = _bridge_request("reconstruct", {})
        exit_code, result = self._run_bridge(request, SimpleNamespace())
        self.assertEqual(exit_code, 1)
        self.assertEqual(result["errors"][0]["code"], "mission_operation_unavailable")
        self.assertEqual(
            result["errors"][0]["missing_owner_primitives"], ["reconstruct"]
        )

    def test_unintegrated_interface_is_honestly_unavailable(self) -> None:
        request = _request("orient", {})
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "orient.json"
            path.write_text(json.dumps(request), encoding="utf-8")
            with patch.object(
                TOOL.MissionInterface, "open", return_value=SimpleNamespace()
            ):
                stream = io.StringIO()
                with redirect_stdout(stream):
                    exit_code = TOOL.main(
                        [
                            "orient",
                            "--workspace-root",
                            "C:\\rh-mission\\workspace",
                            "--project-id",
                            "project.rh",
                            "--mission-id",
                            "mission.1",
                            "--request",
                            str(path.resolve()),
                            "--format",
                            "json",
                        ]
                    )
        self.assertEqual(exit_code, 1)
        payload = json.loads(stream.getvalue())
        self.assertEqual(payload["errors"][0]["code"], "mission_operation_unavailable")
        self.assertEqual(
            payload["errors"][0]["missing_owner_primitives"],
            ["execute_semantic_operation"],
        )


if __name__ == "__main__":
    unittest.main()
