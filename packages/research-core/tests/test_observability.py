from __future__ import annotations

import json
import math
import sys
import unittest
from pathlib import Path
from types import MappingProxyType


PACKAGE_ROOT = Path(__file__).resolve().parents[1]
if str(PACKAGE_ROOT) not in sys.path:
    sys.path.insert(0, str(PACKAGE_ROOT))

from research_core.observability import (  # noqa: E402
    EVENT_KINDS_BY_FAMILY,
    METRIC_KINDS_BY_FAMILY,
    METRIC_UNIT_BY_KIND,
    CaptureWorkspaceObserver,
    EventFamily,
    EventKind,
    MetricFamily,
    MetricKind,
    MetricSample,
    MetricUnit,
    NOOP_WORKSPACE_OBSERVER,
    NoOpWorkspaceObserver,
    ObservationEnvironment,
    ObservationLevel,
    ObservationSafetyError,
    StructuredEvent,
    WorkspaceObserver,
    emit_event_safely,
    observe_metric_safely,
    preparation_progress_sink,
    report_preparation_progress,
    validate_observation_attributes,
    verify_observability_catalog,
)


NOW = "2026-08-04T20:00:00Z"
DIGEST = "a" * 64


class ObservabilityCatalogTests(unittest.TestCase):
    def test_closed_event_catalog_covers_every_locked_architecture_family_once(self) -> None:
        self.assertEqual(
            set(EventFamily),
            {
                EventFamily.COMMAND,
                EventFamily.PROJECT_COMMIT,
                EventFamily.WRITER,
                EventFamily.SQLITE,
                EventFamily.CAS,
                EventFamily.EVIDENCE_CLOSURE,
                EventFamily.RESERVATION_SETTLEMENT,
                EventFamily.ATTEMPT,
                EventFamily.CONTEXT_STALENESS,
                EventFamily.ALERT_HOLD_DELIVERY,
                EventFamily.BACKUP,
                EventFamily.RESTORE,
                EventFamily.MIGRATION,
                EventFamily.COMPATIBILITY,
            },
        )
        members = [kind for kinds in EVENT_KINDS_BY_FAMILY.values() for kind in kinds]
        self.assertEqual(set(members), set(EventKind))
        self.assertEqual(len(members), len(set(members)))
        self.assertTrue(all(EVENT_KINDS_BY_FAMILY[family] for family in EventFamily))
        self.assertEqual(
            EVENT_KINDS_BY_FAMILY[EventFamily.COMMAND],
            frozenset(
                {EventKind.COMMAND_ACCEPTED, EventKind.COMMAND_REJECTED, EventKind.COMMAND_REPLAYED}
            ),
        )
        self.assertEqual(
            EVENT_KINDS_BY_FAMILY[EventFamily.CAS],
            frozenset(
                {
                    EventKind.CAS_STAGED,
                    EventKind.CAS_INSTALLED,
                    EventKind.CAS_VERIFIED,
                    EventKind.CAS_QUARANTINED,
                    EventKind.CAS_SCRUBBED,
                }
            ),
        )
        self.assertEqual(
            EVENT_KINDS_BY_FAMILY[EventFamily.ALERT_HOLD_DELIVERY],
            frozenset(
                {EventKind.ALERT_CHANGED, EventKind.HOLD_CHANGED, EventKind.DELIVERY_INTENT_CHANGED}
            ),
        )
        verify_observability_catalog()

    def test_closed_metric_catalog_covers_every_locked_architecture_family_once(self) -> None:
        self.assertEqual(
            set(MetricFamily),
            {
                MetricFamily.TRANSACTION,
                MetricFamily.WAL,
                MetricFamily.CAS,
                MetricFamily.EVIDENCE_CLOSURE,
                MetricFamily.RESERVATION,
                MetricFamily.ATTEMPT,
                MetricFamily.CONTEXT,
                MetricFamily.HOLD,
                MetricFamily.RECOVERY,
                MetricFamily.BACKUP,
                MetricFamily.PROJECTION,
            },
        )
        members = [kind for kinds in METRIC_KINDS_BY_FAMILY.values() for kind in kinds]
        self.assertEqual(set(members), set(MetricKind))
        self.assertEqual(len(members), len(set(members)))
        self.assertTrue(all(METRIC_KINDS_BY_FAMILY[family] for family in MetricFamily))
        self.assertEqual(set(METRIC_UNIT_BY_KIND), set(MetricKind))
        self.assertEqual(
            METRIC_KINDS_BY_FAMILY[MetricFamily.TRANSACTION],
            frozenset(
                {
                    MetricKind.TRANSACTION_LATENCY_SECONDS,
                    MetricKind.TRANSACTION_CONFLICT_TOTAL,
                    MetricKind.TRANSACTION_BUSY_TOTAL,
                }
            ),
        )
        self.assertEqual(METRIC_UNIT_BY_KIND[MetricKind.WAL_BYTES], MetricUnit.BYTES)
        self.assertEqual(
            METRIC_UNIT_BY_KIND[MetricKind.RESERVATION_UTILIZATION_RATIO], MetricUnit.RATIO
        )
        verify_observability_catalog()


class ObservabilitySafetyTests(unittest.TestCase):
    def test_event_and_metric_emit_only_structured_safe_metadata(self) -> None:
        attributes = {
            "project_id": "project:rh",
            "session_id": "session:S1",
            "root_digest": DIGEST,
            "reason_code": "integrity_check_passed",
            "logical_path": "workspace/session/S1",
            "project_commit": 12,
        }
        event = StructuredEvent(
            EventKind.SQLITE_INTEGRITY_CHECKED,
            NOW,
            ObservationLevel.INFO,
            ObservationEnvironment.CI,
            attributes,
        )
        sample = MetricSample(
            MetricKind.TRANSACTION_LATENCY_SECONDS,
            0.25,
            NOW,
            ObservationEnvironment.CI,
            {"project_id": "project:rh", "latency_seconds": 0.25},
        )
        event_mapping = event.to_mapping()
        metric_mapping = sample.to_mapping()
        self.assertEqual(event_mapping["system"], "mathematical_research")
        self.assertEqual(event_mapping["component"], "research_workspace")
        self.assertEqual(event_mapping["msg"], EventKind.SQLITE_INTEGRITY_CHECKED.value)
        self.assertEqual(metric_mapping["unit"], "seconds")
        json.dumps(event_mapping, sort_keys=True)
        json.dumps(metric_mapping, sort_keys=True)
        self.assertIsInstance(event.attributes, MappingProxyType)
        attributes["reason_code"] = "mutated"
        self.assertEqual(event.attributes["reason_code"], "integrity_check_passed")
        with self.assertRaises(TypeError):
            event.attributes["reason_code"] = "mutated"  # type: ignore[index]

    def test_payload_rejects_proof_prompt_worker_secret_restricted_and_personal_material(self) -> None:
        forbidden = (
            ({"proof_bytes": b"candidate proof"}, "unsafe_attribute_key"),
            ({"prompt": "derive rh"}, "unsafe_attribute_key"),
            ({"worker_output": "raw result"}, "unsafe_attribute_key"),
            ({"credential": "opaque"}, "unsafe_attribute_key"),
            ({"restricted_source": "source:S1"}, "unsafe_attribute_key"),
            ({"name": "Example User"}, "unsafe_attribute_key"),
            ({"email": "person@example.com"}, "unsafe_attribute_key"),
            ({"reason_code": "client_secret=not-real"}, "unsafe_attribute_value"),
            ({"classification": "restricted"}, "sensitive_attribute_value"),
            ({"project_id": "person@example.com"}, "unsafe_attribute_value"),
            ({"reason_code": "call_239_555_0100"}, "sensitive_attribute_value"),
            ({"status": {"raw": "nested"}}, "unsafe_attribute_type"),
            ({"digest_sha256": b"a" * 64}, "unsafe_attribute_type"),
            ({"project_id": "project:rh", 7: "not-a-string-key"}, "unsafe_attribute_key"),
        )
        for payload, expected_code in forbidden:
            with self.subTest(payload=payload):
                with self.assertRaises(ObservationSafetyError) as captured:
                    validate_observation_attributes(payload)
                self.assertEqual(captured.exception.code, expected_code)

    def test_payload_validates_digests_paths_numbers_and_ratios(self) -> None:
        invalid = (
            {"digest_sha256": "A" * 64},
            {"logical_path": "../proof.txt"},
            {"writer_epoch": -1},
            {"count": True},
            {"duration_seconds": math.inf},
            {"coverage_ratio": 1.01},
        )
        for payload in invalid:
            with self.subTest(payload=payload):
                with self.assertRaises(ObservationSafetyError):
                    validate_observation_attributes(payload)

    def test_metric_rejects_nonfinite_negative_and_invalid_ratio_values(self) -> None:
        for value in (math.inf, math.nan, -1, True):
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    MetricSample(MetricKind.CAS_BYTES, value, NOW)
        with self.assertRaises(ValueError):
            MetricSample(MetricKind.BACKUP_COVERAGE_RATIO, 1.001, NOW)
        with self.assertRaises(ValueError):
            MetricSample(MetricKind.CAS_BYTES, 1.5, NOW)

    def test_timestamps_are_explicit_utc_and_enums_are_closed(self) -> None:
        with self.assertRaises(ValueError):
            StructuredEvent(EventKind.PROJECT_COMMITTED, "2026-08-04 20:00:00")
        with self.assertRaises(TypeError):
            StructuredEvent("workspace.project.committed", NOW)  # type: ignore[arg-type]
        with self.assertRaises(TypeError):
            MetricSample("workspace_cas_bytes", 1, NOW)  # type: ignore[arg-type]


class ObserverTests(unittest.TestCase):
    def test_noop_is_default_protocol_conforming_and_has_no_retained_state(self) -> None:
        self.assertIsInstance(NOOP_WORKSPACE_OBSERVER, NoOpWorkspaceObserver)
        self.assertIsInstance(NOOP_WORKSPACE_OBSERVER, WorkspaceObserver)
        event = StructuredEvent(EventKind.COMMAND_ACCEPTED, NOW)
        sample = MetricSample(MetricKind.TRANSACTION_CONFLICT_TOTAL, 0, NOW)
        self.assertIsNone(NOOP_WORKSPACE_OBSERVER.emit_event(event))
        self.assertIsNone(NOOP_WORKSPACE_OBSERVER.observe_metric(sample))
        self.assertFalse(hasattr(NOOP_WORKSPACE_OBSERVER, "events"))
        with self.assertRaises(TypeError):
            NOOP_WORKSPACE_OBSERVER.emit_event({})  # type: ignore[arg-type]

    def test_capture_sink_retains_validated_records_in_order_and_clears(self) -> None:
        observer = CaptureWorkspaceObserver()
        first = StructuredEvent(EventKind.COMMAND_ACCEPTED, NOW)
        second = StructuredEvent(EventKind.PROJECT_COMMITTED, NOW)
        sample = MetricSample(MetricKind.BACKUP_COVERAGE_RATIO, 1.0, NOW)
        observer.emit_event(first)
        observer.emit_event(second)
        observer.observe_metric(sample)
        self.assertEqual(observer.events, (first, second))
        self.assertEqual(observer.metrics, (sample,))
        self.assertIsInstance(observer, WorkspaceObserver)
        with self.assertRaises(TypeError):
            observer.observe_metric({})  # type: ignore[arg-type]
        observer.clear()
        self.assertEqual(observer.events, ())
        self.assertEqual(observer.metrics, ())

    def test_fail_safe_delivery_contains_sink_and_record_construction_failures(self) -> None:
        class FailingObserver:
            def emit_event(self, event):
                raise RuntimeError("sink unavailable")

            def observe_metric(self, sample):
                raise RuntimeError("sink unavailable")

        observer = FailingObserver()
        self.assertFalse(
            emit_event_safely(
                observer,
                kind=EventKind.COMMAND_ACCEPTED,
                occurred_at=NOW,
                attributes={"project_id": "project:rh", "status": "accepted"},
            )
        )
        self.assertFalse(
            observe_metric_safely(
                observer,
                kind=MetricKind.TRANSACTION_LATENCY_SECONDS,
                value=0.1,
                occurred_at=NOW,
                attributes={"project_id": "project:rh"},
            )
        )
        self.assertFalse(
            emit_event_safely(
                NOOP_WORKSPACE_OBSERVER,
                kind=EventKind.COMMAND_ACCEPTED,
                occurred_at=NOW,
                attributes={"proof_bytes": "must never be observable"},
            )
        )


class PreparationProgressTests(unittest.TestCase):
    def test_closed_phases_emit_only_exact_safe_counter_records(self) -> None:
        phases = (
            "logical_digest_rows", "logical_digest_sort_rows", "database_copy", "index_source_ingest",
            "index_occurrence_build", "index_ordinary_build", "index_inverse_build",
            "index_directory_build", "index_audit_sources", "index_audit_occurrences",
            "index_audit_inverses", "source_seal", "source_backup", "source_verify",
            "migration_prepare", "migration_execute", "target_backup", "target_verify",
            "final_invariants", "transition_prepare", "preparation_complete",
        )
        records = []
        maximum = 2 ** 53 - 1
        with preparation_progress_sink(records.append):
            for phase in phases:
                report_preparation_progress(phase)
            report_preparation_progress("index_source_ingest", completed=3, total=9)
            report_preparation_progress("index_source_ingest", completed=0, total=0)
            report_preparation_progress("index_source_ingest", completed=maximum, total=maximum)
        self.assertEqual(records, [
            *({"phase": phase, "completed": 0, "total": None} for phase in phases),
            {"phase": "index_source_ingest", "completed": 3, "total": 9},
            {"phase": "index_source_ingest", "completed": 0, "total": 0},
            {"phase": "index_source_ingest", "completed": maximum, "total": maximum},
        ])
        self.assertTrue(all(type(record) is dict for record in records))

    def test_invalid_progress_is_silently_omitted(self) -> None:
        class IntegerSubclass(int):
            pass

        invalid = [
            {"phase": phase} for phase in ("unknown_phase", "", None, True, [])
        ]
        for field in ("completed", "total"):
            invalid.extend({field: value} for value in (
                True, False, -1, 2 ** 53, 1.0, math.inf, -math.inf, math.nan,
                "1", IntegerSubclass(1),
            ))
        invalid.extend(({"completed": None}, {"completed": 2, "total": 1}))
        records = []
        with preparation_progress_sink(records.append):
            for payload in invalid:
                with self.subTest(payload=payload):
                    report_preparation_progress(**{"phase": "index_source_ingest", **payload})
                    self.assertEqual(records, [])
            report_preparation_progress("index_source_ingest", completed=1)
        self.assertEqual(records, [{"phase": "index_source_ingest", "completed": 1, "total": None}])

    def test_observer_exception_preserves_body_result_and_body_failure(self) -> None:
        calls = []
        result = object()

        def failing_observer(record):
            calls.append(record)
            raise RuntimeError("observer unavailable")

        def body():
            with preparation_progress_sink(failing_observer):
                report_preparation_progress("index_source_ingest", completed=1, total=2)
                return result

        self.assertIs(body(), result)
        failure = ValueError("body failed independently")
        with self.assertRaises(ValueError) as raised:
            with preparation_progress_sink(failing_observer):
                report_preparation_progress("index_source_ingest", completed=2, total=2)
                raise failure
        self.assertIs(raised.exception, failure)
        self.assertEqual([record["completed"] for record in calls], [1, 2])
        report_preparation_progress("index_source_ingest", completed=3)
        self.assertEqual(len(calls), 2)

    def test_nested_contexts_restore_after_normal_and_exceptional_exit(self) -> None:
        outer, inner = [], []
        self.assertIsNone(report_preparation_progress("index_source_ingest"))
        with preparation_progress_sink(outer.append):
            report_preparation_progress("index_source_ingest", completed=1)
            with preparation_progress_sink(inner.append):
                report_preparation_progress("index_source_ingest", completed=2)
            report_preparation_progress("index_source_ingest", completed=3)
            failure = ValueError("nested body failed")
            with self.assertRaises(ValueError) as raised:
                with preparation_progress_sink(inner.append):
                    report_preparation_progress("index_source_ingest", completed=4)
                    raise failure
            self.assertIs(raised.exception, failure)
            report_preparation_progress("index_source_ingest", completed=5)
        self.assertIsNone(report_preparation_progress("index_source_ingest", completed=6))
        self.assertEqual([record["completed"] for record in outer], [1, 3, 5])
        self.assertEqual([record["completed"] for record in inner], [2, 4])

    def test_observer_base_exception_propagates_and_restores_outer_sink(self) -> None:
        outer = []
        stop = KeyboardInterrupt("stop preparation")

        def interrupted_observer(record):
            raise stop

        with preparation_progress_sink(outer.append):
            with self.assertRaises(KeyboardInterrupt) as raised:
                with preparation_progress_sink(interrupted_observer):
                    report_preparation_progress("index_source_ingest")
            self.assertIs(raised.exception, stop)
            report_preparation_progress("index_source_ingest", completed=1)
        report_preparation_progress("index_source_ingest", completed=2)
        self.assertEqual(outer, [{"phase": "index_source_ingest", "completed": 1, "total": None}])


if __name__ == "__main__":
    unittest.main()
