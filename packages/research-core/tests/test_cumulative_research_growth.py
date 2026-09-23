"""Focused, provider-free checks of current Store and index behavior.

These controls exercise the production writer/read paths, current index build
and audit, exact source access, Context update/checkpoint costs, and Capture
affected-set maintenance. They are selected for the relevant changed property;
neither their population sizes nor emitted diagnostics define a release gate.
Retired design-selection comparisons and the broad cumulative-growth bundle
are not repeated here. Scientific oracle tests remain separately owned.

SQL VM steps are sampled at 100-op quantum; statement counts are not row counts
or physical disk IO. Context byte counters identify canonical full-document
arguments to particular real copy, hash and validation calls; they are not
allocation/RSS/physical-IO measurements. Instrumentation serializes selected
Context arguments, so elapsed time is diagnostic, not a bare runtime benchmark.
No provider, service or live Mission is operated by these disposable fixtures.
"""
from __future__ import annotations

from collections import Counter
from collections.abc import Mapping
from contextlib import ExitStack, closing
from dataclasses import replace
import base64
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import sys
import time
import unittest
from unittest import mock


PACKAGE_ROOT = Path(__file__).resolve().parents[1]
TEST_ROOT = Path(__file__).resolve().parent
for path in (PACKAGE_ROOT, TEST_ROOT):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

from research_core import context_revision as context_module
from research_core import executive_orientation as orientation_module
from research_core import mission_interface as interface_module
from research_core import owner_content_access as access_module
from research_core import owner_content_capture as capture_module
from research_core import owner_content_index as index_module
from research_core import owner_content_projection as projection_module
from research_core import workspace_store as store_module
from research_core.evidence_store import EvidenceCAS
from research_core.json_support import canonical_json_bytes
from research_core.mission_evidence import CaptureScope
from research_core.research_model import deep_thaw
from research_core.workspace_store import canonical_raw_capture_id
import test_mission_interface_direct as direct_fixture
import test_executive_orientation as orientation_fixture
import test_mission_retrieval as retrieval_fixture


def _context_document(value):
    return isinstance(value, Mapping) and value.get("kind") == "first_class_context"


class _Work:
    """Bounded aggregates over the complete measured operation, including pages."""

    def __init__(self):
        self.counts = Counter()
        self.source_reads = Counter()
        self.contexts = Counter()
        self.elapsed_seconds = 0.0

    def summary(self):
        return {
            "counts": dict(sorted(self.counts.items())),
            "authenticated_index_source_reads_by_kind": dict(sorted(self.source_reads.items())),
            "context_work": dict(sorted(self.contexts.items())),
            "instrumented_elapsed_seconds": round(self.elapsed_seconds, 6),
        }

    def trace(self, statement):
        self.counts["sql_statements"] += 1
        lowered = statement.lower()
        if re.match(r"\s*(?:insert|update|delete|replace|create|drop|alter|vacuum|reindex)\b", lowered):
            self.counts["write_sql_statements"] += 1
        if "owner_content_index_node" in lowered:
            if re.match(r"\s*select\b", lowered):
                self.counts["index_node_select_statements"] += 1
            elif re.match(r"\s*insert\b", lowered):
                self.counts["index_node_insert_statements"] += 1
        if re.match(r"\s*insert\s+(?:or\s+\w+\s+)?into\s+context_revision\b", lowered):
            self.counts["context_revision_insert_statements"] += 1

    def progress(self):
        self.counts["sql_vm_steps_100_quantum"] += 100
        return 0

    def observe_context(self, operation, value):
        if _context_document(value):
            self.contexts[f"{operation}_calls"] += 1
            # Measurement-only serialization is outside the patched owner hooks.
            self.contexts[f"{operation}_canonical_document_bytes"] += len(canonical_json_bytes(value))


def _measure(operation, *, routine=True):
    work = _Work()
    connect = sqlite3.connect
    source_read = access_module.read_authenticated_source
    typed_validation = store_module._validate_typed_revision_row
    posting_validation = index_module._validate_posting_value
    context_hash = context_module._sha256

    def metered_connect(*args, **kwargs):
        connection = connect(*args, **kwargs)
        work.counts["sqlite_connections"] += 1
        connection.set_trace_callback(work.trace)
        connection.set_progress_handler(work.progress, 100)
        return connection

    def metered_source(*args, **kwargs):
        work.source_reads[str(kwargs["reference"]["kind"])] += 1
        return source_read(*args, **kwargs)

    def metered_validation(object_id, row):
        if object_id.kind.value == "context":
            work.contexts["typed_row_authentication_calls"] += 1
            work.contexts["typed_row_authenticated_payload_bytes"] += len(str(row["payload_json"]).encode("utf-8"))
        return typed_validation(object_id, row)

    def metered_posting(*args, **kwargs):
        work.counts["posting_decode_or_validation_calls"] += 1
        return posting_validation(*args, **kwargs)

    def metered_hash(value):
        work.observe_context("context_owner_hash", value)
        return context_hash(value)

    started = time.perf_counter()
    with ExitStack() as stack:
        stack.enter_context(mock.patch.object(sqlite3, "connect", metered_connect))
        stack.enter_context(mock.patch.object(access_module, "read_authenticated_source", metered_source))
        stack.enter_context(mock.patch.object(store_module, "_validate_typed_revision_row", metered_validation))
        # Returned decoding and internal checks share one validation boundary;
        # count each once even when no outward Posting copy is needed.
        stack.enter_context(mock.patch.object(index_module, "_validate_posting_value", metered_posting))
        stack.enter_context(mock.patch.object(context_module, "_sha256", metered_hash))
        # Counts are deliberately separated by real call site. They must not be
        # summed and reported as distinct documents or physical allocated bytes.
        for name, module in (
            ("context_owner_copy", context_module),
            ("mission_interface_copy", interface_module),
            ("orientation_copy", orientation_module),
        ):
            original = module.deep_thaw

            def metered_copy(value, *, _original=original, _name=name):
                work.observe_context(_name, value)
                return _original(value)

            stack.enter_context(mock.patch.object(module, "deep_thaw", metered_copy))
        if routine:
            stack.enter_context(mock.patch.object(
                store_module.WorkspaceStore, "verify_integrity",
                side_effect=AssertionError("routine operation invoked complete Store audit"),
            ))
        result = operation()
    work.elapsed_seconds = time.perf_counter() - started
    return result, work


class _ResearchFixtureSupport:
    """Shared operations consumed by the focused real-Store checks below."""

    def _report_fixture_progress(self, phase, completed, total):
        # Ordinary unittest has no progress hook. The staging runner validates
        # this exact active fixture and owns its closed labels and totals.
        result = getattr(getattr(self, "_outcome", None), "result", None)
        report = getattr(result, "fixture_progress", None)
        if report is not None:
            report(self, phase, completed, total)

    def _bind_science(self, epoch, root):
        self._fail(epoch)
        mission, _ = self.interface._resolve_owner_selector({"id": "mission:mission.1"})
        self.interface.bind_scientific_context_from_owner(
            expected_mission_revision=mission["revision"],
            expected_mission_payload_sha256=mission["payload_sha256"],
            expected_canonical_authority_digest=self.store.read_metadata()["canonical_authority_digest"],
            context_id=root.record.document["context_id"],
            expected_context_revision=root.revision,
            expected_context_payload_sha256=root.payload_digest,
        )
        return self._successor("cumulative-growth-bound")

    def _append_capture(self, epoch, ordinal, blob):
        observation = f"observation.growthfixture.{ordinal:05d}"
        capture = canonical_raw_capture_id(
            project_id="project.rh", capture_kind="output", observation_id=observation,
        )
        self.store.commit_raw_capture(
            record={
                "capture_id": capture, "mission_id": "mission.1",
                "executive_epoch_id": epoch, "capture_kind": "output",
                "observation_id": observation,
                "assignment_id": f"assignment.growthfixture.{ordinal:05d}",
                "provenance": {"kind": "synthetic_growthfixture"},
                "completion": {"lifecycle": "completed"},
            },
            artifacts=({"ordinal": 0, "role": "result", "logical_name": "growthfixture.txt",
                        "blob_sha256": blob.sha256},),
            blobs=(blob,), lease=self.interface._writer_lease(),
            command_id=f"cumulative-growth.capture.{ordinal:05d}", actor="cumulative-growth-fixture",
        )

    def _all_search_pages(self, epoch, **selection):
        cursor = None
        handles = []
        pages = 0
        min_page_size = max_page_size = 0
        seen_cursors = set()
        while True:
            page = self._query(epoch, "search", **selection, **({"cursor": cursor} if cursor else {}))
            handles.extend(item["id"] for item in page["items"])
            page_size = len(page["items"])
            min_page_size = page_size if pages == 0 else min(min_page_size, page_size)
            max_page_size = max(max_page_size, page_size)
            pages += 1
            cursor = page["next_cursor"]
            if cursor is None:
                self.assertEqual(page["completeness"], "exhausted")
                break
            self.assertNotIn(cursor, seen_cursors, "continuation did not advance")
            seen_cursors.add(cursor)
        self.assertEqual(len(handles), len(set(handles)), "fixed-cut pages repeated owners")
        return {
            "handles": handles, "pages": pages,
            "min_page_size": min_page_size, "max_page_size": max_page_size,
        }

    def _checkpoint_successor(self, epoch, size):
        strategy = self._execute("record_strategy", {
            "mission_continuation": "continue",
            "integrated_comparison": f"Preserve unchanged synthetic science at history size {size:05d}.",
            "reconsideration_conditions": [{"condition": "Only exact synthetic mathematical changes."}],
        }, executive_epoch_id=epoch)
        self.assertEqual(strategy["status"], "completed", strategy)
        checkpoint = self._execute("checkpoint", {}, executive_epoch_id=epoch)
        self.assertEqual(checkpoint["status"], "completed", checkpoint)
        successor = self._successor(f"cumulative-growth-{size}")
        return successor, self.interface.host_snapshot()

    @staticmethod
    def _local_treatment_patch(previous, treatment_id, treatment):
        return {
            "mode": "scientific",
            "updates": [{
                "context_id": f"context:{previous.record.document['context_id']}",
                "expected_head": {
                    "kind": "context", "identity": previous.record.document["context_id"],
                    "revision": previous.revision, "payload_sha256": previous.payload_digest,
                },
                "create": None,
                "patch": {
                    "insert": {}, "replace": {treatment_id: treatment}, "remove": [],
                    "exposure_add": [], "exposure_remove": [], "metadata_replace": {},
                },
            }],
            "exposure_required": [],
        }

    def _publish_local_patch(self, epoch, request):
        result = self._execute("record_context", request, executive_epoch_id=epoch)
        self.assertEqual(result["status"], "completed", result)
        identity = request["updates"][0]["expected_head"]["identity"]
        return context_module.read_context_revision(self.store, context_id=identity)


class IndexDevelopmentControlTests(unittest.TestCase):
    """Small completed codec control through existing Store/index owners."""

    setUpClass = classmethod(direct_fixture.DirectMissionInterfaceTests.setUpClass.__func__)
    setUp = direct_fixture.DirectMissionInterfaceTests.setUp
    _request = staticmethod(direct_fixture.DirectMissionInterfaceTests._request)
    _authorize = direct_fixture.DirectMissionInterfaceTests._authorize
    _bind = direct_fixture.DirectMissionInterfaceTests._bind
    _execute = direct_fixture.DirectMissionInterfaceTests._execute
    _start = retrieval_fixture.RootRetrievalTests._start
    _fail = retrieval_fixture.RootRetrievalTests._fail
    _successor = retrieval_fixture.RootRetrievalTests._successor
    _source_evidence = retrieval_fixture.RootRetrievalTests._source_evidence
    _query = retrieval_fixture.RootRetrievalTests._query
    key = retrieval_fixture.RootRetrievalTests.key
    _scientific_treatment = staticmethod(orientation_fixture.ExecutiveOrientationTests._scientific_treatment)
    _scientific_context = orientation_fixture.ExecutiveOrientationTests._scientific_context
    _all_search_pages = _ResearchFixtureSupport._all_search_pages
    _local_treatment_patch = staticmethod(_ResearchFixtureSupport._local_treatment_patch)
    _publish_local_patch = _ResearchFixtureSupport._publish_local_patch
    _append_capture = _ResearchFixtureSupport._append_capture
    _report_fixture_progress = _ResearchFixtureSupport._report_fixture_progress

    @staticmethod
    def _resources():
        values = {"getrusage_available": 0, "linux_proc_io_available": 0}
        try:
            import resource
        except ImportError:
            pass
        else:
            usage = resource.getrusage(resource.RUSAGE_SELF)
            values.update({
                "getrusage_available": 1,
                "process_peak_rss_bytes": int(usage.ru_maxrss * (1 if sys.platform == "darwin" else 1024)),
                "user_cpu_seconds": usage.ru_utime, "system_cpu_seconds": usage.ru_stime,
                "input_blocks": usage.ru_inblock, "output_blocks": usage.ru_oublock,
            })
        try:
            io_values = dict(line.split(":", 1) for line in Path("/proc/self/io").read_text().splitlines())
            values.update({"linux_" + key: int(io_values[key]) for key in ("read_bytes", "write_bytes", "rchar", "wchar")})
            values["linux_proc_io_available"] = 1
        except (OSError, KeyError, ValueError):
            pass
        return values

    def _phase(self, operation, *, routine=True):
        before = self._resources()
        result, work = _measure(operation, routine=routine)
        after = self._resources()
        availability = ("getrusage_available", "linux_proc_io_available")
        resources = {
            # A phase delta requires both endpoint samples. Unavailable
            # families omit their counters rather than inventing zero cost.
            **{key: min(before[key], after[key]) for key in availability},
            **{key + ("_after" if key == "process_peak_rss_bytes" else "_delta"):
                value if key == "process_peak_rss_bytes" else value - before[key]
               for key, value in after.items() if key in before and key not in availability},
        }
        return result, {**work.summary(), "process_resources": resources}

    def _extent(self, database=None):
        database = self.store.paths.database if database is None else database
        with closing(sqlite3.connect(f"{database.as_uri()}?mode=ro", uri=True)) as connection:
            nodes, payload_bytes = connection.execute(
                "SELECT COUNT(*), COALESCE(SUM(length(CAST(node_json AS BLOB))),0) FROM owner_content_index_node"
            ).fetchone()
            logical_bytes = connection.execute("PRAGMA page_count").fetchone()[0] * connection.execute("PRAGMA page_size").fetchone()[0]
        return {"index_node_rows": nodes, "stored_node_json_bytes": payload_bytes,
                "sqlite_logical_bytes": logical_bytes,
                "database_file_bytes": database.stat().st_size,
                "wal_file_bytes": Path(str(database) + "-wal").stat().st_size if Path(str(database) + "-wal").exists() else 0}

    def _index_context_treatments(self, owner, *, account_codepoints=2048):
        """The unchanged shared-term recipe, without Store publication."""
        treatments = {}
        qualification = "Synthetic finite interface only; uniform extension requires another argument."
        for ordinal in range(8):
            words = " ".join(f"controlcase Straße finite interface term{number:03d}" for number in range(64))
            account = (words * 2)[:account_codepoints]
            if owner == 7 and ordinal == 7:
                account += " only-owner-07"
            treatments[f"part{ordinal}"] = self._scientific_treatment(account, qualifications=[qualification])
        return treatments

    def _construct_index_contexts(self, epoch, owners, *, progress=None):
        records = {}
        for owner in range(owners):
            treatments = self._index_context_treatments(owner)
            identity = f"context.index-control.{owner:02d}"
            records[identity] = self._scientific_context(epoch, identity, treatments)
            if progress is not None:
                progress(owner + 1)
        return records

    def test_small_current_codec_build_query_update_and_independent_audit(self):
        epoch = self._start()
        phases = {}
        extents = {"before": self._extent()}
        records, phases["ordinary_store_construction"] = self._phase(lambda: self._construct_index_contexts(epoch, 8))
        extents["after_construction"] = self._extent()
        fields = {}
        payload_bytes = 0
        projected_bytes = Counter()
        for identity, record in records.items():
            reference = {"kind": "context", "identity": identity, "revision": record.revision,
                         "payload_sha256": record.payload_digest}
            payload_bytes += len(canonical_json_bytes(record.record.document))
            fields[identity] = projection_module.project_root_owner_fields(reference, record.record.document)
            for variant, projected in (
                ("root", fields[identity]),
                ("delegated", projection_module.project_delegated_owner_fields(reference, record.record.document)),
            ):
                projected_bytes[variant] += sum(len(value.encode("utf-8")) for value in projected.values())
        self.assertGreaterEqual(sum(projected_bytes.values()), 256 * 1024)
        self.assertLess(sum(projected_bytes.values()), 384 * 1024)
        # Full Store verification authenticates source inventory independently
        # of the derived index. Do not replace it with graph-only self-checking.
        with mock.patch.object(access_module, "verify_owner_content_relation", wraps=access_module.verify_owner_content_relation) as audit:
            _, phases["independent_store_audit"] = self._phase(self.store.verify_integrity, routine=False)
            self.assertGreater(audit.call_count, 0)

        scratch = Path(self.temporary.name) / "small-index-control.sqlite3"
        cut = int(self.store.read_metadata()["current_project_commit"])

        def with_sources(action, selected_cut=cut):
            with self.store.direct_recovery_read_scope():
                source = self.store._direct_recovery_read_connection.get()
                return action(
                    access_module.iter_authenticated_sources(source, project_id="project.rh", source_project_commit=selected_cut, revision_scope="retained_history"),
                    access_module.iter_authenticated_sources(source, project_id="project.rh", source_project_commit=selected_cut, revision_scope="current_at_cut"),
                )

        def bulk_build():
            with closing(sqlite3.connect(scratch)) as target, target:
                target.row_factory = sqlite3.Row
                # This is the same private node-table shape, not a fake Store or
                # a migration receipt. Only the real bulk index owner runs here.
                target.execute("CREATE TABLE owner_content_index_node (node_digest TEXT PRIMARY KEY NOT NULL CHECK(length(node_digest)=64), node_kind TEXT NOT NULL CHECK(node_kind IN ('range-cell','range-branch','range-source','range-position-vector')), node_json TEXT NOT NULL)")
                target.execute("BEGIN")
                return with_sources(lambda retained, current: access_module.bootstrap_owner_content_index(
                    target, project_id="project.rh", source_project_commit=cut,
                    retained_sources=retained, current_sources=current))

        descriptor, phases["bulk_authenticate_sort_build_commit"] = self._phase(bulk_build)
        extents["bulk_target"] = self._extent(scratch)
        self.assertGreater(extents["bulk_target"]["index_node_rows"], 0)

        def audit_bulk():
            with closing(sqlite3.connect(f"{scratch.as_uri()}?mode=ro", uri=True)) as target:
                target.row_factory = sqlite3.Row
                return with_sources(lambda retained, current: access_module.verify_owner_content_relation(
                    target, descriptor=descriptor, retained_sources=retained, current_sources=current))

        _, phases["independent_bulk_source_audit"] = self._phase(audit_bulk, routine=False)
        queries = {}
        for name, query in (("literal", "finite interface"), ("short_casefold", "ß"),
                            ("rare", "only-owner-07"), ("broad", "controlcase"),
                            ("absent", "absent-control-query-987654321")):
            expected = {f"context:{identity}@1" for identity, projected in fields.items()
                        if query.casefold() in projected["content"].casefold()}
            pages, work = self._phase(lambda: self._all_search_pages(
                epoch, query=query, kinds=["context"], fields=["content"], page_size=3))
            self.assertEqual(set(pages.pop("handles")), expected)
            self.assertEqual(work["counts"].get("write_sql_statements", 0), 0)
            queries[name] = {"source_cut": cut, "returned_items": len(expected), **pages, **work}

        before = records["context.index-control.00"]
        replacement = deep_thaw(before.record.document["treatments"]["part0"])
        replacement["account"] += " local-delta-control"
        request = self._local_treatment_patch(before, "part0", replacement)
        after, phases["one_treatment_delta"] = self._phase(lambda: self._publish_local_patch(epoch, request))
        self.assertEqual(after.revision, before.revision + 1)
        self.assertEqual(after.record.document["treatments"]["part1"], before.record.document["treatments"]["part1"])
        old, phases["retained_exact_revision_read"] = self._phase(lambda: self._query(
            epoch, "read", ids=["context:context.index-control.00@1"]))
        self.assertEqual(old["items"][0]["status"], "readable")
        self.assertNotIn("local-delta-control", old["items"][0]["readable_content"])
        changed = self._all_search_pages(epoch, query="local-delta-control", kinds=["context"], fields=["content"], page_size=3)
        self.assertEqual(changed["handles"], ["context:context.index-control.00@2"])
        _, phases["post_delta_independent_audit"] = self._phase(self.store.verify_integrity, routine=False)
        extents["after_delta"] = self._extent()
        print("CUMULATIVE_INDEX_CONTROL=" + json.dumps({"measurements": [{
            "owners": 8, "treatments_per_owner": 8,
            "unique_context_payload_bytes": payload_bytes,
            "authorized_projected_field_bytes": dict(projected_bytes),
            "initial_current_plus_retained_field_membership_bytes": 2 * sum(projected_bytes.values()),
            "bulk_index_node_rows_including_genesis": extents["bulk_target"]["index_node_rows"],
            "small_patch_request_bytes": len(canonical_json_bytes(request)),
            "phases": phases, "queries": queries, "extents": extents,
            }],
            "limits": [
                "Synthetic ~256 KiB authorized-field development rung; no production source-shape or scale qualification.",
                "Ordinary construction includes real owner publication; bulk build is a fresh private index from authenticated real Store sources, not an offline migration or deployment proof.",
                "Bulk scope includes small genesis owners; its node-row count measures physical stored rows, not logical memberships or the five-role manifest count. Reported Context payload/field bytes describe only the eight added Contexts.",
                "Instrumented timings include counters and source authentication; cache state is uncontrolled and phases share one process.",
                "Peak RSS is process-lifetime high water after each phase, not phase allocation. Block counters are OS blocks; Linux IO counters are process observations, not corpus size.",
                "Extent inspection is outside phase timing/resource deltas; database/node totals include retained historical roots and exclude temporary sort peak storage.",
                "This control establishes only its exercised Store/index properties, not a performance threshold, provider experiment or production preparation.",
            ],
        }, sort_keys=True), flush=True)

    def test_64_owner_current_codec_and_multiway_revision_growth(self):
        """Named development selection; raw unittest discovery includes it."""
        started = time.perf_counter()
        epoch = self._start()
        self._report_fixture_progress(0, 0, 64)
        records, construction = self._phase(lambda: self._construct_index_contexts(
            epoch, 64, progress=lambda completed: self._report_fixture_progress(0, completed, 64)))
        extents = {"after_construction": self._extent()}
        phases, cuts, inventories, summaries, queries, revisions = {}, [], [], [], {}, []
        identity = "context.index-control.00"

        def inventory():
            cut = int(self.store.read_metadata()["current_project_commit"])
            with self.store.direct_recovery_read_scope():
                source = self.store._direct_recovery_read_connection.get()
                result = tuple((scope, item)
                    for scope in ("retained_history", "current_at_cut")
                    for item in access_module.iter_authenticated_sources(source, project_id="project.rh",
                        source_project_commit=cut, revision_scope=scope))
            return cut, result

        def source_summary(cut, sources):
            counts = Counter(source_projections=len(sources))
            owners, owner_revisions = set(), set()
            for scope, source in sources:
                owners.add((source.kind, source.identity))
                owner_revisions.add((source.kind, source.identity, source.revision))
                counts[scope + "_source_projections"] += 1
                for variant, fields in source.fields.items():
                    size = sum(len(text.encode("utf-8")) for text in fields.values())
                    counts[scope + "_" + variant + "_field_bytes"] += size
                    counts[scope + "_field_bytes"] += size
                    counts[scope + "_fields"] += len(fields)
                    if source.kind == "context":
                        counts[scope + "_context_field_bytes"] += size
                    affected = "affected_owner" if (source.kind, source.identity) == ("context", identity) else "unrelated_owners"
                    counts[scope + "_" + affected + "_field_bytes"] += size
            return {"source_cut": cut, "unique_owners": len(owners), "unique_owner_revisions": len(owner_revisions),
                    "current_plus_retained_field_bytes": counts["current_at_cut_field_bytes"] + counts["retained_history_field_bytes"],
                    **dict(counts)}

        def capture(label):
            (cut, sources), phases[label + "_source_authentication"] = self._phase(inventory)
            cuts.append(cut)
            inventories.append(sources)
            summaries.append(source_summary(cut, sources))
            return cut, sources

        def public_queries(label, sources, cases, *, progress=None):
            current = {source.handle: source for scope, source in sources
                       if scope == "current_at_cut" and source.kind == "context"}
            observed = {}
            for query_number, (name, text) in enumerate(cases, 1):
                expected = {key for key, source in current.items()
                            if text.casefold() in source.fields["root"]["content"].casefold()}
                result, cost = self._phase(lambda: self._all_search_pages(
                    epoch, query=text, kinds=["context"], fields=["content"], page_size=3))
                self.assertEqual(set(result.pop("handles")), expected)
                self.assertEqual(cost["counts"].get("write_sql_statements", 0), 0)
                observed[name] = {"source_cut": cuts[-1], "returned_items": len(expected), **result, **cost,
                                  "elapsed_seconds_per_page": cost["instrumented_elapsed_seconds"] / result["pages"]}
                if progress is not None:
                    progress(query_number)
            queries[label] = observed

        self._report_fixture_progress(1, 0, 1)
        _, initial = capture("initial")
        actual_initial_bytes = summaries[0]["current_at_cut_context_field_bytes"]
        self.assertGreaterEqual(actual_initial_bytes, 2 * 1024 * 1024)
        self.assertLess(actual_initial_bytes, 3 * 1024 * 1024)
        self._report_fixture_progress(1, 1, 1)
        self._report_fixture_progress(2, 0, 5)
        public_queries("initial", initial, (
            ("literal", "finite interface"), ("short_casefold", "ß"),
            ("rare", "only-owner-07"), ("broad", "controlcase"),
            ("absent", "absent-control-query-987654321"),
        ), progress=lambda completed: self._report_fixture_progress(2, completed, 5))
        before = records[identity]
        old_records = [before]
        self._report_fixture_progress(3, 0, 3)
        for ordinal, label in enumerate(("local_append", "equal_length", "offset_shift"), 1):
            replacement = deep_thaw(before.record.document["treatments"]["part0"])
            previous_account = replacement["account"]
            if label == "local_append":
                replacement["account"] += " local-delta-control"
            elif label == "equal_length":
                self.assertIn("term000", previous_account)
                replacement["account"] = previous_account.replace("term000", "term999", 1)
                self.assertEqual(len(replacement["account"].encode("utf-8")), len(previous_account.encode("utf-8")))
            else:
                replacement["account"] = "offset-shift-control " + previous_account
            request = self._local_treatment_patch(before, "part0", replacement)
            after, cost = self._phase(lambda: self._publish_local_patch(epoch, request))
            self.assertEqual(after.revision, before.revision + 1)
            self.assertEqual(
                {key: value for key, value in after.record.document["treatments"].items() if key != "part0"},
                {key: value for key, value in before.record.document["treatments"].items() if key != "part0"},
            )
            self.assertEqual(
                {key: value for key, value in after.record.document["treatments"]["part0"].items() if key != "account"},
                {key: value for key, value in before.record.document["treatments"]["part0"].items() if key != "account"},
            )
            cut, sources = capture(label)
            # Assert equality of actual authenticated unrelated sources, not
            # just an unchanged source count or assumed owner boundary.
            def unrelated(items):
                return {(scope, source.kind, source.identity, source.revision): source for scope, source in items
                        if (source.kind, source.identity) != ("context", identity)}
            self.assertEqual(unrelated(inventories[-2]), unrelated(sources))
            revisions.append({"ordinal": ordinal, "before_revision": before.revision, "after_revision": after.revision,
                "before_cut": cuts[-2], "after_cut": cut, "patch_request_bytes": len(canonical_json_bytes(request)),
                "before_account_bytes": len(previous_account.encode("utf-8")),
                "after_account_bytes": len(replacement["account"].encode("utf-8")),
                "unrelated_authenticated_sources_equal": 1, "store_update": cost})
            extents[label] = self._extent()
            cases = [("updated", "local-delta-control"), ("rare", "only-owner-07")]
            if ordinal >= 2:
                cases.append(("equal_length", "term999"))
            if ordinal == 3:
                cases.append(("offset_shift", "offset-shift-control"))
            public_queries(label, sources, cases)
            old_records.append(after)
            before = after
            self._report_fixture_progress(3, ordinal, 3)
        # One final real Store audit covers its committed graph/history;
        # source equality alone does not establish this property.
        self._report_fixture_progress(4, 0, 1)
        _, phases["final_independent_store_audit"] = self._phase(self.store.verify_integrity, routine=False)
        self._report_fixture_progress(4, 1, 1)
        def retained_reads():
            for read_number, record in enumerate(old_records, 1):
                result = self._query(epoch, "read", ids=[f"context:{identity}@{record.revision}"])
                self.assertEqual(result["items"][0]["status"], "readable")
                body = result["items"][0]["readable_content"]
                self.assertEqual("local-delta-control" in body, record.revision >= 2)
                self.assertEqual("term999" in body, record.revision >= 3)
                self.assertEqual("offset-shift-control" in body, record.revision >= 4)
                self._report_fixture_progress(5, read_number, 4)
        self._report_fixture_progress(5, 0, 4)
        _, phases["retained_exact_revision_reads"] = self._phase(retained_reads)
        preparation_elapsed = time.perf_counter() - started
        print("CUMULATIVE_INDEX_SCALE_CONTROL=" + json.dumps({"measurements": [{
            "owners": 64, "treatments_per_owner": 8, "account_characters_before_markers": 2048,
            "fixture_preparation_elapsed_seconds": preparation_elapsed,
            "ordinary_store_construction": construction, "source_cuts": summaries,
            "store_revisions": revisions, "store_queries": queries, "store_extents": extents,
            "source_phases": phases, "total_elapsed_seconds": time.perf_counter() - started,
        }], "limits": [
            "Explicit 64-owner current-Store control, not a production workload or accepted SLO.",
            "Actual authenticated source/field bytes include genesis/current/retained/root/delegated distinctions; anticipated size is not input evidence.",
            "One real Store construction carries three same-owner revisions and their public queries; retired physical-layout comparison builds are absent.",
            "The final Store audit and exact historical reads check the actual committed graph and owner history.",
            "Equal-length correction and early insertion separate revision changes from offset/length changes. Other authenticated owners and exact qualifications stay unchanged.",
            "Page counts and per-page averages accompany totals; broad queries legitimately return more owners.",
            "Instrumentation and retained source inventories add cost. Timings and process-lifetime peak RSS are diagnostic, not isolated production latency or allocation.",
        ]}, sort_keys=True), flush=True)

    def test_single_1mib_context_update_checkpoint_cost(self):
        """Named development control; raw unittest discovery also includes it."""
        size = 1024 * 1024
        marker = "UNRELATED-LARGE-CONTEXT:"
        account = marker + "x" * (size - len(marker))
        selected = self._scientific_treatment(
            "The selected finite treatment remains small.",
            qualifications=["Normalization: pi; fixed degree only."],
        )
        epoch = self._start()
        phases, extents = {}, {"initial": self._extent()}
        self._report_fixture_progress(0, 0, 1)
        source, phases["source_publication_and_owner_read"] = self._phase(
            lambda: self._scientific_context(epoch, "context.large-control.source", {
                "used": selected, "unrelated": self._scientific_treatment(account),
            }),
        )
        self.assertEqual(len(account.encode("utf-8")), size)
        original_bytes = len(canonical_json_bytes(source.record.document))
        self.assertGreaterEqual(original_bytes, size)
        self._report_fixture_progress(0, 1, 1)
        pinned, _ = self.interface._resolve_owner_selector({"id": "context:context.large-control.source"})
        self._report_fixture_progress(1, 0, 1)
        root, phases["parent_publication_and_owner_read"] = self._phase(
            lambda: self._scientific_context(epoch, "context.large-control.parent", {
                "parent": self._scientific_treatment("Use only the selected finite treatment.", sources=[
                    orientation_fixture.ExecutiveOrientationTests._scientific_source(
                        pinned, selection={"mode": "treatments", "treatment_ids": ["used"]},
                    ),
                ]),
            }),
        )
        self._report_fixture_progress(1, 1, 1)
        self._report_fixture_progress(2, 0, 1)
        epoch, phases["science_binding_and_setup_successor"] = self._phase(
            lambda: _ResearchFixtureSupport._bind_science(self, epoch, root),
        )
        extents["after_publication_and_binding"] = self._extent()
        self._report_fixture_progress(2, 1, 1)
        before_read = deep_thaw(self.store.read_metadata())
        self._report_fixture_progress(3, 0, 1)
        initial, phases["ordinary_orientation_before_patch"] = self._phase(self.interface.executive_orientation)
        self.assertNotIn(marker, json.dumps(initial["scientific_context"]))
        self.assertEqual(initial["scientific_context"]["source_changes"], [])
        self.assertEqual(phases["ordinary_orientation_before_patch"]["counts"].get("write_sql_statements", 0), 0)
        self.assertEqual(deep_thaw(self.store.read_metadata()), before_read)
        self._report_fixture_progress(3, 1, 1)

        replacement = {**selected, "account": "The selected finite treatment has one local authored refinement."}
        request = self._local_treatment_patch(source, "used", replacement)
        request_bytes = len(canonical_json_bytes(request))
        self.assertLess(request_bytes, 4096)
        self.assertNotIn(marker, json.dumps(request))
        self._report_fixture_progress(4, 0, 1)
        updated, phases["small_patch_and_owner_read"] = self._phase(
            lambda: self._publish_local_patch(epoch, request),
        )
        self.assertEqual(updated.revision, source.revision + 1)
        self.assertTrue(updated.record.document["treatments"]["unrelated"] == source.record.document["treatments"]["unrelated"])
        self.assertEqual(deep_thaw(updated.record.document["treatments"]["used"]), replacement)
        updated_bytes = len(canonical_json_bytes(updated.record.document))
        update_cost = phases["small_patch_and_owner_read"]
        self.assertGreaterEqual(update_cost["context_work"]["context_owner_copy_canonical_document_bytes"], updated_bytes)
        self.assertGreaterEqual(update_cost["context_work"]["context_owner_hash_canonical_document_bytes"], updated_bytes)
        self.assertGreaterEqual(update_cost["counts"]["context_revision_insert_statements"], 1)
        extents["after_small_patch"] = self._extent()
        self._report_fixture_progress(4, 1, 1)
        before_read = deep_thaw(self.store.read_metadata())
        self._report_fixture_progress(5, 0, 1)
        corrected, phases["ordinary_orientation_after_patch"] = self._phase(self.interface.executive_orientation)
        science = corrected["scientific_context"]
        self.assertNotIn(marker, json.dumps(science))
        self.assertEqual(len(science["source_changes"]), 1)
        change = science["source_changes"][0]
        self.assertEqual(change["cited_reference"], pinned)
        self.assertEqual(change["state"], "advanced_same_identity")
        self.assertEqual(change["selection"], {"mode": "treatments", "treatment_ids": ["used"]})
        self.assertEqual(change["current_reference"], {
            "kind": "context", "identity": "context.large-control.source",
            "revision": updated.revision, "payload_sha256": updated.payload_digest,
        })
        self.assertEqual(change["qualification"]["treatments"], {"used": replacement})
        self.assertEqual(change["qualification"]["context_qualifications"], {
            field: deep_thaw(updated.record.document[field])
            for field in ("known_omissions", "restricted_uses", "restrictions", "independence_treatment")
        })
        self.assertGreaterEqual(
            phases["ordinary_orientation_after_patch"]["context_work"]["typed_row_authenticated_payload_bytes"],
            updated_bytes,
        )
        self.assertEqual(phases["ordinary_orientation_after_patch"]["counts"].get("write_sql_statements", 0), 0)
        self.assertEqual(deep_thaw(self.store.read_metadata()), before_read)
        self._report_fixture_progress(5, 1, 1)

        # Observe real owner calls, never replace source publication with a
        # fabricated image or infer that a completed checkpoint copied a DB.
        original_copy = self.store._copy_quiesced_store_to_checkpoint_source_stage
        original_digest = self.store._checkpoint_source_digest
        original_handoff = self.store.publish_checkpoint_source_handoff
        transfer = Counter()
        handoffs = []

        def observed_copy(**kwargs):
            stage_descriptor = kwargs["stage_descriptor"]
            source_descriptors = set()
            original_open, original_read, original_write = os.open, os.read, os.write

            def opened(path, *args, **options):
                descriptor = original_open(path, *args, **options)
                if Path(path) == self.store.paths.database:
                    source_descriptors.add(descriptor)
                return descriptor

            def read(descriptor, count):
                raw = original_read(descriptor, count)
                if descriptor in source_descriptors:
                    transfer["database_copy_read_bytes"] += len(raw)
                elif descriptor == stage_descriptor:
                    transfer["stage_readback_bytes"] += len(raw)
                return raw

            def write(descriptor, data):
                written = original_write(descriptor, data)
                if descriptor == stage_descriptor:
                    transfer["stage_written_bytes"] += written
                return written

            started = time.perf_counter()
            with mock.patch.object(os, "open", opened), mock.patch.object(os, "read", read), mock.patch.object(os, "write", write):
                result = original_copy(**kwargs)
            transfer["copy_calls"] += 1
            transfer["copy_including_wal_checkpoint_seconds"] += time.perf_counter() - started
            return result

        def observed_digest(*args, **kwargs):
            started = time.perf_counter()
            result = original_digest(*args, **kwargs)
            transfer["snapshot_digest_calls"] += 1
            transfer["snapshot_digest_verified_bytes"] += result[1]
            transfer["snapshot_digest_seconds"] += time.perf_counter() - started
            return result

        def observed_handoff(**kwargs):
            result = original_handoff(**kwargs)
            handoffs.append((result, kwargs))
            return result

        checkpoint_epoch = epoch
        self._report_fixture_progress(6, 0, 1)
        with (
            mock.patch.object(self.store, "_copy_quiesced_store_to_checkpoint_source_stage", side_effect=observed_copy),
            mock.patch.object(self.store, "_checkpoint_source_digest", side_effect=observed_digest),
            mock.patch.object(self.store, "publish_checkpoint_source_handoff", side_effect=observed_handoff),
        ):
            (epoch, snapshot), phases["strategy_checkpoint_handoff_successor_snapshot"] = self._phase(
                lambda: _ResearchFixtureSupport._checkpoint_successor(self, epoch, size),
            )
        self.assertEqual(len(handoffs), 1)
        handoff, binding = handoffs[0]
        self.assertEqual(handoff.status, "published")
        self.assertTrue(handoff.checkpoint_completed)
        self.assertIsNone(handoff.operation_failure_code)
        self.assertIsNotNone(handoff.source)
        self.assertIsNotNone(handoff.successor_lease)
        self.assertEqual(handoff.source.checkpoint_id, binding["checkpoint_id"])
        self.assertEqual(handoff.source.checkpoint_sha256, binding["checkpoint_sha256"])
        self.assertEqual(handoff.source.checkpoint_project_commit, binding["expected_project_commit"])
        self.assertEqual(handoff.source.byte_length, handoff.database_bytes)
        self.assertEqual(transfer["copy_calls"], 1)
        for key in ("database_copy_read_bytes", "stage_written_bytes", "stage_readback_bytes"):
            self.assertEqual(transfer[key], handoff.database_bytes)
        self.assertGreaterEqual(transfer["snapshot_digest_calls"], 1)
        self.assertGreaterEqual(transfer["snapshot_digest_verified_bytes"], handoff.database_bytes)
        checkpoint = self.store.read_continuation_checkpoint(executive_epoch_id=checkpoint_epoch)
        self.assertEqual(checkpoint["checkpoint_id"], handoff.source.checkpoint_id)
        self.assertEqual(checkpoint["payload_digest"], handoff.source.checkpoint_sha256)
        self.assertNotEqual(epoch, checkpoint_epoch)
        self.assertEqual(snapshot["executive_orientation"]["scientific_context"], science)
        extents["after_checkpoint_and_successor"] = self._extent()
        extents["published_checkpoint_source"] = self._extent(handoff.source.path)
        self.assertEqual(extents["published_checkpoint_source"]["database_file_bytes"], handoff.database_bytes)
        self._report_fixture_progress(6, 1, 1)

        before_read = deep_thaw(self.store.read_metadata())
        self._report_fixture_progress(7, 0, 1)
        retained, phases["retained_source_revision_read_after_successor"] = self._phase(
            lambda: context_module.read_context_revision(
                self.store, context_id="context.large-control.source", revision=source.revision,
            ),
        )
        self.assertEqual(retained.payload_digest, source.payload_digest)
        self.assertTrue(retained.record.document == source.record.document)
        self.assertEqual(phases["retained_source_revision_read_after_successor"]["counts"].get("write_sql_statements", 0), 0)
        self.assertEqual(deep_thaw(self.store.read_metadata()), before_read)
        self._report_fixture_progress(7, 1, 1)
        print("CUMULATIVE_LARGE_CONTEXT_CONTROL=" + json.dumps({"measurements": [{
            "unrelated_treatment_account_utf8_bytes": size,
            "initial_context_canonical_bytes": original_bytes,
            "updated_context_canonical_bytes": updated_bytes,
            "local_patch_request_utf8_bytes": request_bytes,
            "scoped_qualification_utf8_bytes": len(canonical_json_bytes(change["qualification"])),
            "before_revision": source.revision, "after_revision": updated.revision,
            "checkpoint_project_commit": handoff.source.checkpoint_project_commit,
            "checkpoint_source_bytes": handoff.database_bytes,
            "checkpoint_handoff_elapsed_ms": handoff.handoff_elapsed_ms,
            "checkpoint_transfer": dict(transfer), "phases": phases, "extents": extents,
            }], "limits": [
                "One synthetic Context with a repetitive 1 MiB unrelated account, one small selected treatment and one citing parent; field-size/position-list cost, not lexical diversity, Capture history, corpus-scale, 8 MiB or production qualification.",
                "Real public publication, orientation, patch, checkpoint source handoff and successor owners execute. Successful source publication is asserted separately from semantic checkpoint completion.",
                "Context byte counters attribute repeated full-document copy/hash/validation arguments, not unique data or allocation. Instrumentation adds serialization and callback overhead.",
                "Copy byte counters observe actual Python descriptor reads/writes inside the existing copy owner; later snapshot-digest bytes are returned verified extents from that real owner. Neither measures SQLite native IO or physical disk traffic.",
                "Copy timing includes WAL checkpointing. Handoff and whole checkpoint/successor timings contain nested work; do not sum them or combine repeated byte counts as unique DB size.",
                "Database/index extents are measured outside operation timing; WAL truncation and the checkpoint source image are distinct from semantic Context bytes.",
                "Process peak RSS is lifetime high water, not phase allocation; unavailable resource counters remain explicit. Cache state and OS IO effects are uncontrolled.",
                "Real positional index maintenance is included in Context publication and patch; a 1 MiB account does not imply a 1 MiB index or database image.",
                "This named control measures its one Context/update/checkpoint case; it is not a corpus-scale or release-completion claim.",
            ]}, sort_keys=True), flush=True)

    def test_small_latest_output_amplification(self):
        epoch = self._start()
        cas = EvidenceCAS(self.store.paths)
        blob = replace(cas.ingest_bytes(
            b"Synthetic navigation discriminator; no mathematical claim.",
            original_name="growthfixture.txt", media_type="text/plain", encoding="utf-8",
        ).record, availability_state="verified_available")
        latest_outputs = []
        ordinal = 0
        cursor_key = "cd" * 32

        def observe(**request):
            return self.interface.observe_mission(request, cursor_mac_key=cursor_key)

        def capture_id(number):
            return canonical_raw_capture_id(project_id="project.rh", capture_kind="output",
                observation_id=f"observation.growthfixture.{number:05d}")

        # Exactly 8 then 32 old outputs, with two selected-Epoch outputs in
        # each measured cut. Fixture construction/succession is not timed as a
        # read. No assumed ordering advantage from hashed Capture identities.
        for old_outputs in (8, 32):
            while ordinal < old_outputs:
                self._append_capture(epoch, ordinal, blob)
                ordinal += 1
            self._fail(epoch)
            epoch = self._successor(f"navigation-control-{old_outputs}")
            selected = {f"capture:{capture_id(number)}" for number in range(ordinal, ordinal + 2)}
            for number in range(ordinal, ordinal + 2):
                self._append_capture(epoch, number, blob)
            ordinal += 2
            count = Counter()
            original_page = self.store.read_observation_capture_page

            def metered_page(**kwargs):
                batch = original_page(**kwargs)
                count["capture_page_calls"] += 1
                count["capture_records_inspected"] += len(batch["records"])
                return batch

            def read_outputs():
                result_handles = []
                binding = cursor = None
                seen = set()
                while True:
                    selection = {} if binding is None else {"source_binding": binding}
                    if cursor is not None:
                        selection["cursor"] = cursor
                    result = observe(operation="catalog", collection="latest_outputs", page_size=4, **selection)
                    self.assertIn(result["state"], ("present", "empty"))
                    count["response_pages"] += 1
                    if not result["items"]:
                        count["empty_response_pages"] += 1
                    elif "capture_records_to_first_result" not in count:
                        count["capture_records_to_first_result"] = count["capture_records_inspected"]
                    result_handles.extend(item["value"]["handle"] for item in result["items"])
                    binding, cursor = result["source_binding"], result["next_cursor"]
                    if cursor is None:
                        return result_handles
                    self.assertNotIn(cursor, seen)
                    seen.add(cursor)

            before = deep_thaw(self.store.read_metadata())
            with mock.patch.object(self.store, "read_observation_capture_page", side_effect=metered_page):
                handles, work = self._phase(read_outputs)
            self.assertEqual(set(handles), selected)
            self.assertEqual(len(handles), len(selected))
            self.assertEqual(deep_thaw(self.store.read_metadata()), before)
            self.assertEqual(work["counts"].get("write_sql_statements", 0), 0)
            latest_outputs.append({"old_output_captures": old_outputs, "selected_output_captures": 2,
                "requested_page_size": 4, "returned_items": len(handles),
                "inspected_to_returned_ratio": count["capture_records_inspected"] / len(handles),
                **dict(count), **work})

        print("CUMULATIVE_LATEST_OUTPUT_CONTROL=" + json.dumps({"measurements": [{
            "latest_outputs": latest_outputs,
            }], "limits": [
                "Synthetic 8/32 old Capture fixtures only; no production-scale or source-shape claim.",
                "Read phases exclude fixture construction and succession.",
                "Capture records inspected counts authenticated batch descriptors before latest-output filtering, not underlying index nodes; SQL/index counters report separate work.",
                "Instrumented cache-warm process observations are diagnostic; peak RSS is process-lifetime high water and OS IO counters may reflect caching.",
                "Exact selected-Epoch output membership, pagination and read-only Store metadata remain asserted; no retrieval policy or index format changes here.",
            ]}, sort_keys=True), flush=True)

    def test_small_exact_source_amplification(self):
        epoch = self._start()
        cas = EvidenceCAS(self.store.paths)
        exact_sources = []
        ordinal = 0
        cursor_key = "cd" * 32

        def observe(**request):
            return self.interface.observe_mission(request, cursor_mac_key=cursor_key)

        def capture_id(number):
            return canonical_raw_capture_id(project_id="project.rh", capture_kind="output",
                observation_id=f"observation.growthfixture.{number:05d}")

        for size in (4096, 65536, 2 * 1024 * 1024 + 257):
            requested_bytes = 1024 if size <= 65536 else 65536
            raw = (b"Finite case only; exact qualifications remain available.\n" * (size // 20 + 1))[:size]
            blob = replace(cas.ingest_bytes(raw, original_name="growthfixture.txt",
                media_type="text/plain", encoding="utf-8").record, availability_state="verified_available")
            self._append_capture(epoch, ordinal, blob)
            identity = capture_id(ordinal)
            ordinal += 1
            scope = {"description": "Synthetic finite case only.", "excluded": "uniform statement"}
            evidence = self._source_evidence(epoch, f"evidence.navigation-control.{size}",
                sources=(CaptureScope(identity, 0, scope),))
            self._source_evidence(epoch, evidence.evidence_id,
                sources=(CaptureScope(identity, 0, {"description": "A distinct later scope."}),),
                previous=evidence)
            binding = observe(operation="current_decision")["source_binding"]
            selected_path = cas.path_for_digest(blob.sha256)
            original_open = Path.open
            original_descriptor_open, original_fdopen = os.open, os.fdopen
            selected_descriptors: set[int] = set()
            count = Counter()

            class MeteredBlobStream:
                def __init__(self, stream):
                    self.stream = stream

                def __enter__(self):
                    self.stream.__enter__()
                    return self

                def __exit__(self, *args):
                    return self.stream.__exit__(*args)

                def read(self, *args, **kwargs):
                    requested = args[0] if args else kwargs.get("size", -1)
                    self_outer.assertGreater(requested, 0)
                    self_outer.assertEqual(requested, size + 1)
                    count["largest_blob_read_request"] = max(count["largest_blob_read_request"], requested)
                    value = self.stream.read(*args, **kwargs)
                    count["selected_blob_read_calls"] += 1
                    count["selected_blob_bytes_read"] += len(value)
                    return value

                def __getattr__(self, name):
                    return getattr(self.stream, name)

            def metered_open(path, *args, **kwargs):
                stream = original_open(path, *args, **kwargs)
                if path == selected_path:
                    count["selected_blob_opens"] += 1
                    return MeteredBlobStream(stream)
                return stream

            def metered_descriptor_open(path, *args, **kwargs):
                descriptor = original_descriptor_open(path, *args, **kwargs)
                if Path(path) == selected_path:
                    count["selected_blob_opens"] += 1
                    selected_descriptors.add(descriptor)
                return descriptor

            def metered_fdopen(descriptor, *args, **kwargs):
                stream = original_fdopen(descriptor, *args, **kwargs)
                if descriptor in selected_descriptors:
                    selected_descriptors.remove(descriptor)
                    return MeteredBlobStream(stream)
                return stream

            def read_source():
                offset, pieces = 0, []
                while offset is not None:
                    result = observe(operation="exact_record", handle=f"capture-artifact:{identity}#0",
                        source_evidence=f"evidence:{evidence.evidence_id}@1", source_ordinal=0,
                        source_binding=binding, offset_bytes=offset, max_bytes=requested_bytes)
                    self.assertEqual(result["state"], "present")
                    page = result["items"][0]
                    chunk = base64.b64decode(page["content_base64"], validate=True)
                    self.assertEqual(page["page_sha256"], hashlib.sha256(chunk).hexdigest())
                    self.assertEqual(page["sha256"], blob.sha256)
                    self.assertEqual(page["source_relation"]["exact_scope"], scope)
                    self.assertEqual(page["source_relation"]["source_ordinal"], 0)
                    self.assertEqual(page["source_relation"]["evidence_reference"]["revision"], 1)
                    self.assertEqual(page["offset_bytes"], offset)
                    self.assertEqual(page["total_bytes"], size)
                    self.assertEqual(page["returned_bytes"], len(chunk))
                    self.assertLessEqual(len(chunk), requested_bytes)
                    count["response_pages"] += 1
                    count["returned_bytes"] += len(chunk)
                    pieces.append(chunk)
                    next_offset = page["next_offset_bytes"]
                    if next_offset is not None:
                        self.assertGreater(next_offset, offset)
                    offset = next_offset
                return b"".join(pieces)

            before = deep_thaw(self.store.read_metadata())
            self_outer = self
            with mock.patch.object(Path, "open", metered_open), mock.patch.object(
                os, "open", metered_descriptor_open,
            ), mock.patch.object(os, "fdopen", metered_fdopen):
                returned, work = self._phase(read_source)
            self.assertEqual(returned, raw)
            self.assertEqual(count["response_pages"], (size + requested_bytes - 1) // requested_bytes)
            self.assertEqual(count["selected_blob_opens"], count["response_pages"])
            self.assertEqual(count["selected_blob_bytes_read"], size * count["response_pages"])
            self.assertEqual(work["counts"].get("write_sql_statements", 0), 0)
            self.assertEqual(deep_thaw(self.store.read_metadata()), before)
            exact_sources.append({"source_bytes": size, "requested_page_bytes": requested_bytes,
                "selected_blob_read_to_returned_ratio": count["selected_blob_bytes_read"] / size,
                **dict(count), **work})

        print("CUMULATIVE_EXACT_SOURCE_CONTROL=" + json.dumps({"measurements": [{
            "exact_sources": exact_sources,
            }], "limits": [
                "Synthetic 4/64 KiB plus 2 MiB+257-byte sources in a fresh workspace, with three Captures and six Evidence revisions; no production-scale or source-shape claim.",
                "Source pages reuse one full selected-Blob snapshot per page. Canonical whole-buffer secret classification retains O(Blob size) memory; neither chunk/page allocation nor page-proportional IO is established.",
                "Read phases exclude fixture construction, source publication and initial source-binding lookup. Unrelated latest-output history is not constructed; elapsed/SQL costs are not directly comparable with the prior combined fixture.",
                "Selected-blob bytes count actual Python stream reads, including security/authentication reads, not physical disk IO or unique bytes.",
                "Instrumented cache-warm process observations are diagnostic; peak RSS is process-lifetime high water and OS IO counters may reflect caching.",
                "The caller retains fixture bytes and reassembles all pages; process RSS does not isolate server page working memory.",
                "Exact bytes, scopes, historical revision binding, page digests and read-only Store metadata remain asserted; no retrieval policy or index format changes here.",
            ]}, sort_keys=True), flush=True)


class CaptureCheckpointMaintenanceTests(unittest.TestCase):
    """Real-Store regression for exact Capture checkpoint affected sets."""

    setUpClass = classmethod(direct_fixture.DirectMissionInterfaceTests.setUpClass.__func__)
    setUp = direct_fixture.DirectMissionInterfaceTests.setUp
    _request = staticmethod(direct_fixture.DirectMissionInterfaceTests._request)
    _authorize = direct_fixture.DirectMissionInterfaceTests._authorize
    _bind = direct_fixture.DirectMissionInterfaceTests._bind
    _execute = direct_fixture.DirectMissionInterfaceTests._execute
    _start = retrieval_fixture.RootRetrievalTests._start
    _successor = retrieval_fixture.RootRetrievalTests._successor
    _append_capture = _ResearchFixtureSupport._append_capture
    _checkpoint_successor = _ResearchFixtureSupport._checkpoint_successor

    def test_checkpoint_successor_updates_only_new_epoch_captures_and_preserves_fixed_cuts(self):
        epoch = self._start()
        blob = replace(EvidenceCAS(self.store.paths).ingest_bytes(
            b"Synthetic checkpoint cost discriminator; no mathematical claim.",
            original_name="growthfixture.txt", media_type="text/plain", encoding="utf-8",
        ).record, availability_state="verified_available")
        original_projection = capture_module.capture_projection_changes
        original_issued = capture_module._issued_capture
        measurements = []
        fixed_cuts = []
        previous_current = {}
        all_captures = set()
        ordinal = 0

        def descriptors_at(cut):
            with self.store.direct_recovery_read_scope():
                page = self.store.read_owner_content_search_page(
                    mission_id="mission.1", projection_variant="root", kinds=("capture",),
                    fields=("content",), query="growthfixture", source_project_commit=cut,
                    revision_scope="current_at_cut", page_size=10,
                )
            self.assertFalse(page["has_more"])
            self.assertIsNone(page["next_after"])
            return {str(item["reference"]["identity"]): deep_thaw(item["capture_descriptor"])
                    for item in page["items"]}

        for new_count in (2, 3):
            expected_new = {
                canonical_raw_capture_id(project_id="project.rh", capture_kind="output",
                    observation_id=f"observation.growthfixture.{number:05d}")
                for number in range(ordinal, ordinal + new_count)
            }

            def append_batch():
                for number in range(ordinal, ordinal + new_count):
                    self._append_capture(epoch, number, blob)

            _, append_work = _measure(append_batch)
            ordinal += new_count
            all_captures.update(expected_new)
            cut = int(self.store.read_metadata()["current_project_commit"])
            before = descriptors_at(cut)
            self.assertEqual(set(before), all_captures)
            for identity in expected_new:
                self.assertEqual(before[identity]["origin_epoch_state"], "epoch_not_terminal")
                self.assertEqual(before[identity]["cut_relation"],
                                 "no_checkpoint" if new_count == 2 else "after_latest_checkpoint")
            fixed_cuts.append((cut, before))
            issued = Counter()
            changed = Counter()

            def observed_issued(connection, capture_id, **kwargs):
                issued[capture_id] += 1
                return original_issued(connection, capture_id, **kwargs)

            def observed_projection(*args, **kwargs):
                changes = original_projection(*args, **kwargs)
                changed.update(after.identity for _before, after in changes)
                return changes

            with (
                mock.patch.object(capture_module, "_issued_capture", observed_issued),
                mock.patch.object(capture_module, "capture_projection_changes", observed_projection),
            ):
                (epoch, _snapshot), checkpoint_work = _measure(
                    lambda: self._checkpoint_successor(epoch, ordinal),
                )
            # Recomputing every old Capture and merely discarding unchanged
            # descriptors must fail, as must writing duplicate/new occurrences.
            expected_calls = Counter({identity: 1 for identity in expected_new})
            self.assertEqual(issued, expected_calls, checkpoint_work.summary())
            self.assertEqual(changed, expected_calls, checkpoint_work.summary())
            current = descriptors_at(int(self.store.read_metadata()["current_project_commit"]))
            self.assertEqual(set(current), all_captures)
            for identity, descriptor in previous_current.items():
                self.assertEqual(current[identity], descriptor)
            for identity in expected_new:
                self.assertEqual(current[identity]["origin_epoch_state"], "checkpointed")
                self.assertEqual(current[identity]["cut_relation"], "at_or_before_latest_checkpoint")
            for fixed_cut, expected in fixed_cuts:
                self.assertEqual(descriptors_at(fixed_cut), expected)
            _, audit_work = _measure(self.store.verify_integrity, routine=False)
            measurements.append({
                "new_captures": new_count, "retained_captures": len(all_captures),
                "authenticated_previous_captures": sum(issued.values()),
                "changed_capture_projections": sum(changed.values()),
                "append": append_work.summary(),
                "strategy_checkpoint_authorization_binding_successor_snapshot": checkpoint_work.summary(),
                "full_audit": audit_work.summary(),
            })
            previous_current = current

        # Counts diagnose this exact affected-set regression, not corpus scale.
        print("CAPTURE_CHECKPOINT_DIAGNOSTIC=" + json.dumps(measurements, sort_keys=True), flush=True)


if __name__ == "__main__":
    unittest.main()
