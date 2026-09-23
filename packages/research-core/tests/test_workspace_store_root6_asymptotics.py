from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import sys
import tempfile
import time
import unittest
from contextlib import ExitStack, closing
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Callable, TypeVar
from unittest.mock import patch


PACKAGE_ROOT = Path(__file__).resolve().parents[1]
TEST_ROOT = Path(__file__).resolve().parent
REPO_ROOT = Path(__file__).resolve().parents[3]
for candidate in (PACKAGE_ROOT, TEST_ROOT):
    if str(candidate) not in sys.path:
        sys.path.insert(0, str(candidate))

from research_core.canonical_snapshot import load_canonical_snapshot  # noqa: E402
from research_core import owner_content_access as content_access  # noqa: E402
from research_core import owner_content_capture as content_capture  # noqa: E402
from research_core import workspace_store as store_module  # noqa: E402
from research_core.candidate_revision import (  # noqa: E402
    candidate_a1_requirement,
    candidate_schema_digest_v4,
)
from research_core.evidence_store import EvidenceCAS  # noqa: E402
from research_core.mission_evidence import (  # noqa: E402
    CaptureScope,
    commit_capture_scope_annotation,
    commit_evidence_meaning,
    prepare_capture_scope_annotation,
    prepare_evidence_meaning,
    read_evidence_meaning,
)
from research_core.mission_interface import MissionInterface  # noqa: E402
from research_core.mission_operation_contract import (  # noqa: E402
    CHECKPOINT,
    MISSION_SEMANTIC_REQUEST_SCHEMA_VERSION,
    RETRIEVE,
)
from research_core.mission_executive import (  # noqa: E402
    OwnerRevisionRef,
    prepare_direct_executive_epoch_authorized_event,
    prepare_direct_executive_epoch_bound_event,
    prepare_direct_executive_epoch_failed_before_checkpoint_event,
    reissue_direct_executive_epoch_authority,
)
from research_core.workspace_paths import (  # noqa: E402
    WorkspacePaths,
    attest_current_principal,
    stable_principal_owner_binding,
)
from research_core.workspace_schema import (  # noqa: E402
    IdentityKind,
    TypedWorkspaceId,
    canonical_payload,
)
from research_core.workspace_store import (  # noqa: E402
    _AUXILIARY_SPECS,
    WorkspaceIntegrityError,
    WorkspaceStore,
    canonical_raw_capture_id,
)
from research_core_test_support import (  # noqa: E402
    independent_root6_body_at_commit,
    independent_root6_digest,
    independent_root6_member_mutations,
)
from test_workspace_store_successor_writes import _direct_genesis_fixture  # noqa: E402


_T = TypeVar("_T")
def _CLOCK():
    return "2026-09-07T00:00:00Z"


_HISTORY_TABLE = re.compile(
    r"\b(?:project_commit|transition_journal|command_result|writer_epoch|"
    r"current_dependency_node|current_dependency_projection|"
    r"capture_artifact_coverage_node|root_hook_index_node|"
    r"\w+_revision|\w+_head|"
    + "|".join(re.escape(table.value) for table in _AUXILIARY_SPECS)
    + r")\b",
    re.IGNORECASE,
)


class _HistoryFixture:
    """A real Store history; no copied journal rows or bypassed validators."""

    def __init__(self, root: Path, canonical: object):
        self.paths = WorkspacePaths.from_root(root)
        self.store = WorkspaceStore.initialize_direct_mission_workspace(
            self.paths,
            project_id="project.rh",
            canonical_snapshot=canonical,
            actor="root6-fixture",
            clock=_CLOCK,
        )
        self.paths = self.store.paths
        metadata = self.store.read_metadata()
        self.authority_digest = str(metadata["canonical_authority_digest"])
        self.principal = attest_current_principal()
        self.lease = self.store.claim_writer(
            owner=stable_principal_owner_binding(self.principal),
            creation_basis="root6-focused-fixture",
            expected_project_commit=int(metadata["current_project_commit"]),
            expected_root_digest=str(metadata["current_root_digest"]),
            expected_canonical_authority_digest=self.authority_digest,
        )
        self.store.initialize_direct_mission_genesis(
            _direct_genesis_fixture(),
            mission_id="mission.1",
            canonical_snapshot=canonical,
            lease=self.lease,
            command_id="root6.genesis",
            actor="root6-fixture",
        )
        mission = self.store.get_head(TypedWorkspaceId(IdentityKind.MISSION, "mission.1"))
        assert mission is not None
        self.epoch_id = "epoch.root6.fixture"
        authorized = prepare_direct_executive_epoch_authorized_event(
            executive_epoch_id=self.epoch_id,
            mission_root=OwnerRevisionRef(
                "mission", "mission.1", mission.reference.revision, mission.payload_digest
            ),
            predecessor_checkpoint=None,
        )
        authorization_metadata = self.store.read_metadata()
        self.store.authorize_executive_epoch(
            mission_id="mission.1",
            executive_epoch_id=self.epoch_id,
            event=authorized,
            lease=self.lease,
            command_id="root6.epoch.authorize",
            actor="root6-fixture",
            expected_canonical_authority_digest=self.authority_digest,
            expected_mission_host_store_cut={
                "project_commit": int(authorization_metadata["current_project_commit"]),
                "current_root_digest": str(authorization_metadata["current_root_digest"]),
                "transition_head_digest": authorization_metadata["transition_head_digest"],
                "canonical_authority_digest": str(
                    authorization_metadata["canonical_authority_digest"]
                ),
            },
        )
        self.store.bind_executive_epoch(
            mission_id="mission.1",
            executive_epoch_id=self.epoch_id,
            event=prepare_direct_executive_epoch_bound_event(
                executive_epoch_id=self.epoch_id,
                mission_id="mission.1",
                goal_thread_id="goal-thread:root6-fixture",
                workspace_root=str(root.resolve()),
            ),
            expected_event_ordinal=1,
            expected_event_digest=authorized.digest_sha256,
            lease=self.lease,
            command_id="root6.epoch.bind",
            actor="root6-fixture",
            expected_canonical_authority_digest=self.authority_digest,
        )
        self.authority = reissue_direct_executive_epoch_authority(
            event_readbacks=self.store.read_active_executive_epoch("mission.1"),
            project_id="project.rh",
            root_identity=self.store.paths.root_identity,
            canonical_authority_digest=self.authority_digest,
        )
        authority_sha256 = self.authority.authority_sha256
        candidate_id = "candidate.root6.current-a1"
        candidate_payload = {
            "schema_version": 4,
            "kind": "candidate_revision",
            "contract_schema_sha256": candidate_schema_digest_v4(REPO_ROOT),
            "candidate_id": candidate_id,
            "mission_id": "mission.1",
            "proposal_kind": "mathematical_statement",
            "exact_statement": "Synthetic complete-target Candidate for Store scaling only.",
            "scope_and_reach": "synthetic complete unconditional proof of RH",
            "complete_target_claim": {
                "target": "riemann_hypothesis",
                "disposition": "proof",
            },
            "standing": {
                "status": "open",
                "basis": "synthetic unverified complete-target Candidate",
            },
            "provenance": {
                "kind": "native_executive_epoch",
                "executive_epoch_id": self.epoch_id,
                "executive_epoch_authority_sha256": authority_sha256,
            },
            "authority_class": "candidate_only",
            "canonical_effect": "none",
        }
        self.store.commit_candidate_revision(
            executive_epoch_id=self.epoch_id,
            mission_id="mission.1",
            candidate_id=candidate_id,
            payload=candidate_payload,
            expected_head_revision=None,
            expected_head_payload_digest=None,
            a1_requirement=candidate_a1_requirement(candidate_payload),
            lease=self.lease,
            command_id="root6.candidate.current-a1",
            actor="root6-fixture",
            expected_canonical_authority_digest=self.authority_digest,
        )
        self.commit = int(self.store.read_metadata()["current_project_commit"])
        self.branches = []
        self.captures = []
        self.covering_evidence = None
        self.blob = replace(
            EvidenceCAS(self.paths).ingest_bytes(
                b"A synthetic retained observation, with no mathematical claim.",
                original_name="observation.txt",
                media_type="text/plain",
                encoding="utf-8",
            ).record,
            availability_state="verified_available",
        )

    def append_branch(self):
        suffix = f"{self.commit + 1:06d}"
        return self.append_named_branch(f"branch.root6.{suffix}")

    def append_named_branch(self, branch_id: str):
        suffix = f"{self.commit + 1:06d}"
        result = self.store.commit_branch_revision(
            executive_epoch_id=self.epoch_id,
            mission_id="mission.1",
            branch_id=branch_id,
            payload={
                "branch_id": branch_id,
                "mission_id": "mission.1",
                "meaning": "Synthetic Store scaling fixture; no mathematical claim.",
            },
            expected_head_revision=None,
            expected_head_payload_digest=None,
            lease=self.lease,
            command_id=f"root6.branch.{suffix}",
            actor="root6-fixture",
            expected_canonical_authority_digest=self.authority_digest,
        )
        self.commit = result.project_commit
        self.branches.append(result.changed_heads[0])
        return result

    def append_capture(self, *, artifact_count: int = 1):
        suffix = f"{self.commit + 1:06d}"
        observation_id = f"observation.root6.{suffix}"
        capture_id = canonical_raw_capture_id(
            project_id="project.rh", capture_kind="output", observation_id=observation_id
        )
        result = self.store.commit_raw_capture(
            record={
                "capture_id": capture_id,
                "mission_id": "mission.1",
                "executive_epoch_id": self.epoch_id,
                "capture_kind": "output",
                "observation_id": observation_id,
                "assignment_id": f"assignment.root6.{suffix}",
                "provenance": {"kind": "synthetic_store_scaling_fixture"},
                "completion": {"lifecycle": "completed"},
            },
            artifacts=tuple(
                {
                    "ordinal": ordinal,
                    "role": "result",
                    "logical_name": f"observation-{ordinal}.txt",
                    "blob_sha256": self.blob.sha256,
                }
                for ordinal in range(artifact_count)
            ),
            blobs=(self.blob,),
            lease=self.lease,
            command_id=f"root6.capture.{suffix}",
            actor="root6-fixture",
        )
        self.commit = result.project_commit
        self.captures.append(capture_id)
        return result

    def advance_branch(self, branch_id: str):
        object_id = TypedWorkspaceId(IdentityKind.BRANCH, branch_id)
        head = self.store.get_head(object_id)
        assert head is not None
        result = self.store.commit_branch_revision(
            executive_epoch_id=self.epoch_id,
            mission_id="mission.1",
            branch_id=branch_id,
            payload={
                "branch_id": branch_id,
                "mission_id": "mission.1",
                "meaning": (
                    "Synthetic advanced Store scaling fixture revision "
                    f"{head.reference.revision + 1}; no claim."
                ),
            },
            expected_head_revision=head.reference.revision,
            expected_head_payload_digest=head.payload_digest,
            lease=self.lease,
            command_id=f"root6.branch.advance.{branch_id}.{self.commit + 1}",
            actor="root6-fixture",
            expected_canonical_authority_digest=self.authority_digest,
        )
        self.commit = result.project_commit
        return result

    def grow_to(self, project_commits: int):
        while self.commit < project_commits:
            if (self.commit + 1) % 2:
                self.append_capture()
            else:
                self.append_branch()

    def cover_capture_prefix(self, count: int):
        selected = tuple(self.captures[:count])
        if len(selected) != count:
            raise AssertionError("coverage fixture lacks its requested Captures")
        prior = self.covering_evidence
        record = prepare_evidence_meaning(
            authority=self.authority,
            evidence_id="evidence.root6.coverage-scale",
            statement=f"The first {count} exact captured artifacts were reviewed.",
            exact_scope=f"the first {count} exact Capture artifacts",
            strength="exact_finite_identity",
            semantic_role="result",
            authority_basis="direct review of each exact captured artifact",
            sources=tuple(
                CaptureScope(
                    capture_id,
                    0,
                    {"coverage": "complete artifact"},
                )
                for capture_id in selected
            ),
            non_inferences=("does not establish a mathematical theorem",),
            expected_head_revision=(None if prior is None else prior.revision),
            expected_head_payload_digest=(
                None if prior is None else prior.payload_digest
            ),
        )
        outcome = commit_evidence_meaning(
            self.store,
            record=record,
            lease=self.lease,
            actor="root6-fixture",
        )
        self.commit = outcome.project_commit
        self.covering_evidence = read_evidence_meaning(
            self.store,
            evidence_id=record.evidence_id,
        )
        return outcome


@dataclass
class _SqlWork:
    statements: list[str] = field(default_factory=list)
    vm_steps: int = 0
    elapsed_seconds: float = 0
    retain_statements: bool = True
    statement_count: int = 0

    def trace(self, statement: str):
        self.statement_count += 1
        if self.retain_statements:
            self.statements.append(statement)

    def progress(self):
        self.vm_steps += 100
        return 0

    def summary(self):
        return {
            "statements": self.statement_count,
            "vm_steps_100_quantum": self.vm_steps,
            "elapsed_seconds": round(self.elapsed_seconds, 6),
        }


def _measure(
    operation: Callable[[], _T], *, routine: bool, retain_statements: bool = True,
    work: _SqlWork | None = None,
) -> tuple[_T, _SqlWork]:
    if work is None:
        work = _SqlWork(retain_statements=retain_statements)
    connect = sqlite3.connect

    def instrumented_connect(*args, **kwargs):
        connection = connect(*args, **kwargs)
        connection.set_trace_callback(work.trace)
        connection.set_progress_handler(work.progress, 100)
        return connection

    started = time.perf_counter()
    with ExitStack() as stack:
        stack.enter_context(patch.object(sqlite3, "connect", instrumented_connect))
        if routine:
            stack.enter_context(patch.object(
                WorkspaceStore,
                "verify_integrity",
                side_effect=AssertionError("routine root6 called the complete audit"),
            ))
        result = operation()
    work.elapsed_seconds = time.perf_counter() - started
    return result, work


class Root6SqlMeasurementTests(unittest.TestCase):
    def test_count_only_measurement_preserves_work_and_results(self):
        def operation():
            with closing(sqlite3.connect(":memory:")) as connection:
                connection.execute("CREATE TABLE measurement (value INTEGER PRIMARY KEY)")
                connection.execute(
                    "WITH RECURSIVE sequence(value) AS ("
                    "VALUES (1) UNION ALL SELECT value + 1 FROM sequence WHERE value < 128"
                    ") INSERT INTO measurement SELECT value FROM sequence"
                )
                result = connection.execute("SELECT COUNT(*), SUM(value) FROM measurement").fetchone()
                connection.commit()
                return result

        supplied = _SqlWork()
        retained_result, retained = _measure(operation, routine=False, work=supplied)
        self.assertIs(retained, supplied)
        counted_result, counted = _measure(operation, routine=False, retain_statements=False)
        self.assertEqual(retained_result, (128, 8256))
        self.assertEqual(counted_result, retained_result)
        self.assertGreater(len(retained.statements), 0)
        self.assertEqual(retained.summary()["statements"], len(retained.statements))
        self.assertEqual(counted.statements, [])
        self.assertGreater(retained.vm_steps, 0)
        for metric in ("statements", "vm_steps_100_quantum"):
            self.assertEqual(counted.summary()[metric], retained.summary()[metric], metric)


class Root6AsymptoticTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        loaded = load_canonical_snapshot(REPO_ROOT, source_commit="a" * 40)
        if not loaded.ok or loaded.value is None:
            raise AssertionError(loaded.failure)
        cls.canonical = loaded.value

    def test_ordinary_write_matches_literal_ten_key_root6_formula(self):
        with tempfile.TemporaryDirectory() as temporary:
            fixture = _HistoryFixture(
                Path(temporary) / "ordinary-root6-formula",
                self.canonical,
            )
            outcome = fixture.append_branch()
            with fixture.store._connection(read_only=True) as connection:
                contract = store_module.verify_schema_contract(
                    connection,
                    schema_version=10,
                )
                body, stored_root = independent_root6_body_at_commit(
                    connection,
                    schema_contract={
                        "schema_version": contract.schema_version,
                        "migration_set_digest": contract.migration_digest,
                        "schema_object_digest": contract.schema_object_digest,
                    },
                    project_commit_no=outcome.project_commit,
                )
                journal_digest = connection.execute(
                    "SELECT digest_sha256 FROM transition_journal "
                    "WHERE project_commit_no = ?",
                    (outcome.project_commit,),
                ).fetchone()

            self.assertEqual(len(body), 10)
            self.assertEqual(
                body["predecessor_project_commit_no"],
                outcome.project_commit - 1,
            )
            self.assertIsNotNone(body["predecessor_root_digest"])
            self.assertIsNotNone(body["predecessor_transition_head_digest"])
            self.assertIsNotNone(body["transition_head_digest"])
            self.assertIsNotNone(journal_digest)
            assert journal_digest is not None
            self.assertEqual(body["transition_head_digest"], journal_digest[0])
            self.assertEqual(independent_root6_digest(body), stored_root)

            nulled_members = set()
            for member, mutation in independent_root6_member_mutations(body):
                with self.subTest(member=member):
                    self.assertNotEqual(
                        independent_root6_digest(mutation),
                        stored_root,
                    )
                if mutation[member] is None:
                    nulled_members.add(member)
            self.assertEqual(
                nulled_members,
                {
                    "predecessor_project_commit_no",
                    "predecessor_root_digest",
                    "predecessor_transition_head_digest",
                    "transition_head_digest",
                },
            )

    def _assert_routine_plans(self, fixture: _HistoryFixture, work: _SqlWork):
        forbidden = [
            statement for statement in work.statements
            if re.search(r"PRAGMA\s+(?:\w+\.)?(?:integrity_check|foreign_key_check)\b", statement, re.I)
        ]
        self.assertFalse(forbidden, forbidden)
        with closing(sqlite3.connect(fixture.paths.database)) as connection:
            for statement in set(work.statements):
                if not statement.lstrip().upper().startswith("SELECT"):
                    continue
                if not _HISTORY_TABLE.search(statement):
                    continue
                plans = [str(row[3]) for row in connection.execute("EXPLAIN QUERY PLAN " + statement)]
                # An ordered index endpoint with LIMIT 1 consumes one entry.
                endpoint = (
                    bool(re.search(r"\bLIMIT\s+1\s*$", statement, re.I))
                    and not re.search(r"\bWHERE\b", statement, re.I)
                    and not any("TEMP B-TREE" in plan.upper() for plan in plans)
                )
                scans = [plan for plan in plans if "SCAN " in plan.upper()]
                bounded_index_page = (
                    bool(re.search(r"\bLIMIT\s+\d+\s*$", statement, re.I))
                    and scans
                    and all("USING" in plan.upper() and "INDEX" in plan.upper() for plan in scans)
                    and not any("TEMP B-TREE" in plan.upper() for plan in plans)
                )
                self.assertTrue(
                    not scans or endpoint or bounded_index_page,
                    (statement, plans),
                )

    def test_one_write_validates_each_shared_origin_delta_once(self):
        with tempfile.TemporaryDirectory() as temporary:
            fixture = _HistoryFixture(Path(temporary) / "workspace", self.canonical)
            dependencies = {}
            with closing(sqlite3.connect(fixture.paths.database)) as connection:
                connection.row_factory = sqlite3.Row
                for kind, table in (
                    ("mission", "mission_head"),
                    ("branch", "branch_head"),
                    ("strategy", "strategy_head"),
                ):
                    row = connection.execute(
                        f"SELECT object_id, revision, payload_digest, project_commit_no "
                        f"FROM {table}"
                    ).fetchone()
                    self.assertEqual(int(row["project_commit_no"]), 1)
                    dependencies[f"{kind}:{row['object_id']}"] = (
                        int(row["revision"]),
                        str(row["payload_digest"]),
                    )

            with patch.object(
                store_module,
                "_validated_transition_journal_row",
                wraps=store_module._validated_transition_journal_row,
            ) as validate_journal:
                fixture.store.commit_branch_revision(
                    executive_epoch_id=fixture.epoch_id,
                    mission_id="mission.1",
                    branch_id="branch.root6.shared-origin",
                    payload={
                        "branch_id": "branch.root6.shared-origin",
                        "mission_id": "mission.1",
                        "meaning": "Exercise one operation-local shared-origin validation.",
                    },
                    expected_head_revision=None,
                    expected_head_payload_digest=None,
                    dependency_heads=dependencies,
                    lease=fixture.lease,
                    command_id="root6.branch.shared-origin",
                    actor="root6-fixture",
                    expected_canonical_authority_digest=fixture.authority_digest,
                )

            validated_commits = tuple(
                int(call.args[0]["project_commit_no"])
                for call in validate_journal.call_args_list
            )
            self.assertEqual(validated_commits.count(1), 1, validated_commits)
            self.assertIsNone(store_module._ACTIVE_COMMIT_ENVELOPE_CACHE.get())

    def test_compact_checkpoint_creation_does_not_traverse_retained_history(self):
        observations = []
        with tempfile.TemporaryDirectory() as temporary:
            for size in (16, 32, 64):
                fixture = _HistoryFixture(
                    Path(temporary) / f"workspace-{size}", self.canonical
                )
                fixture.grow_to(size)
                interface = MissionInterface(
                    store=fixture.store,
                    cas=EvidenceCAS(fixture.paths),
                    expected_mission_id="mission.1",
                    canonical_snapshot=self.canonical,
                )
                request = {
                    "schema_version": MISSION_SEMANTIC_REQUEST_SCHEMA_VERSION,
                    "operation": CHECKPOINT,
                    "input": {},
                }
                work = _SqlWork()
                projection_work = _SqlWork(retain_statements=False)
                index_work = _SqlWork(retain_statements=False)
                projection_calls = 0
                index_calls = 0
                expected_captures = set(fixture.captures)
                original_projection = content_capture.capture_projection_changes
                original_index_delta = content_access.apply_owner_content_delta

                def measured_maintenance(operation, measured):
                    statements, steps = work.statement_count, work.vm_steps
                    started = time.perf_counter()
                    try:
                        return operation()
                    finally:
                        measured.statement_count += work.statement_count - statements
                        measured.vm_steps += work.vm_steps - steps
                        measured.elapsed_seconds += time.perf_counter() - started

                def capture_projection(*args, **kwargs):
                    nonlocal projection_calls
                    projection_calls += 1
                    changes = measured_maintenance(
                        lambda: original_projection(*args, **kwargs), projection_work,
                    )
                    self.assertEqual(len(changes), len(expected_captures))
                    self.assertEqual({after.identity for _before, after in changes}, expected_captures)
                    for before, after in changes:
                        self.assertIsNotNone(before)
                        self.assertEqual(before.identity, after.identity)
                        self.assertEqual(before.kind, "capture")
                        self.assertEqual(after.kind, "capture")
                        self.assertEqual(set(before.fields), {"root"})
                        self.assertEqual(set(after.fields), {"root"})
                    return changes

                def capture_index_delta(*args, **kwargs):
                    nonlocal index_calls
                    index_calls += 1
                    delta = kwargs["delta"]
                    # This checkpoint has no other owner-content change. Only
                    # these exact, necessary Capture memberships may be metered
                    # separately from compact checkpoint work.
                    self.assertFalse(delta.pending_capture_projection)
                    self.assertEqual(delta.removed_retained, ())
                    for sources in (delta.removed_current, delta.added_current, delta.added_retained):
                        self.assertEqual(len(sources), len(expected_captures))
                        self.assertEqual({source.identity for source in sources}, expected_captures)
                        for source in sources:
                            self.assertEqual(source.kind, "capture")
                            self.assertEqual(set(source.fields), {"root"})
                    return measured_maintenance(
                        lambda: original_index_delta(*args, **kwargs), index_work,
                    )

                with (
                    patch.object(content_capture, "capture_projection_changes", capture_projection),
                    patch.object(content_access, "apply_owner_content_delta", capture_index_delta),
                    patch.object(
                        interface,
                        "_current_open_candidate_a1",
                        return_value=(),
                    ),
                    patch.object(
                        interface,
                        "_pending_capture_locators",
                        side_effect=AssertionError(
                            "compact checkpoint enumerated Capture history"
                        ),
                    ),
                    patch.object(
                        interface,
                        "_resolve_direct_checkpoint_owner",
                        side_effect=AssertionError(
                            "compact checkpoint traversed its owner closure"
                        ),
                    ),
                    patch.object(
                        store_module,
                        "_validated_journal_index",
                        side_effect=AssertionError(
                            "compact checkpoint built the complete journal index"
                        ),
                    ),
                ):
                    result, work = _measure(
                        lambda: interface.execute_semantic_operation(
                            request,
                            executive_epoch_id=fixture.epoch_id,
                        ),
                        routine=True,
                        work=work,
                    )
                self.assertEqual(result["status"], "completed", result)
                self.assertEqual((projection_calls, index_calls), (1, 1))
                self.assertGreater(projection_work.statement_count, 0)
                self.assertGreater(index_work.statement_count, 0)
                checkpoint = fixture.store.read_continuation_checkpoint(
                    executive_epoch_id=fixture.epoch_id
                )
                self.assertIsNotNone(checkpoint)
                assert checkpoint is not None
                self.assertEqual(checkpoint["document"]["schema_version"], 2)
                self.assertLess(
                    len(json.dumps(checkpoint["document"], sort_keys=True)),
                    1024,
                )
                _, audit_work = _measure(fixture.store.verify_integrity, routine=False,
                                         retain_statements=False)
                self.assertFalse(
                    any(
                        "SELECT * FROM transition_journal ORDER BY sequence_no"
                        in statement
                        for statement in work.statements
                    ),
                    work.statements,
                )
                self._assert_routine_plans(fixture, work)
                compact_work = _SqlWork(
                    retain_statements=False,
                    statement_count=work.statement_count - projection_work.statement_count - index_work.statement_count,
                    vm_steps=work.vm_steps - projection_work.vm_steps - index_work.vm_steps,
                    elapsed_seconds=work.elapsed_seconds - projection_work.elapsed_seconds - index_work.elapsed_seconds,
                )
                self.assertGreater(compact_work.statement_count, 0)
                self.assertGreaterEqual(compact_work.vm_steps, 0)
                self.assertGreaterEqual(compact_work.elapsed_seconds, 0)
                observations.append({
                    "history_size": size, "affected_captures": len(expected_captures),
                    "total_checkpoint": work.summary(),
                    "capture_projection": projection_work.summary(),
                    "capture_index_delta": index_work.summary(),
                    "compact_checkpoint": compact_work.summary(),
                    "full_audit": audit_work.summary(),
                })

        print("ROOT6_COMPACT_CHECKPOINT_MEASUREMENTS=" + json.dumps(observations))
        statement_counts = [item["compact_checkpoint"]["statements"] for item in observations]
        # First-checkpoint/terminal Capture descriptors really change. Their
        # fully executed, exact-identity-checked maintenance is reported above;
        # it is not a claim of constant total checkpoint cost. The compact
        # checkpoint itself must still avoid retained-history traversal, and
        # every SQL plan (including maintenance) remains checked above.
        self.assertLessEqual(
            max(statement_counts) - min(statement_counts),
            6,
            observations,
        )
        steps = [item["compact_checkpoint"]["vm_steps_100_quantum"] for item in observations]
        self.assertLessEqual(max(steps), min(steps) * 1.15 + 1000, observations)

    def test_exact_historical_origin_and_seek_page_do_not_build_history_index(self):
        observations = []
        with tempfile.TemporaryDirectory() as temporary:
            for size in (24, 64):
                fixture = _HistoryFixture(
                    Path(temporary) / f"retrieval-{size}", self.canonical
                )
                created = fixture.append_branch()
                historical = created.changed_heads[0]
                fixture.advance_branch(historical.object_id.value)
                fixture.grow_to(size)

                def retrieve():
                    with fixture.store.direct_recovery_read_scope():
                        exact = fixture.store.read_retained_history_record(
                            source_family="branches",
                            identity=historical.object_id.value,
                            revision=historical.revision,
                            cut_project_commit=fixture.commit,
                        )
                        first = fixture.store.read_retained_history_page(
                            source_families=("branches", "captures"),
                            cut_project_commit=fixture.commit,
                            after=None,
                            limit=2,
                        )
                        second = fixture.store.read_retained_history_page(
                            source_families=("branches", "captures"),
                            cut_project_commit=fixture.commit,
                            after=first["next_after"],
                            limit=2,
                        )
                    return exact, first, second

                with (
                    patch.object(
                        store_module,
                        "_validated_journal_index",
                        side_effect=AssertionError(
                            "targeted retrieval built the complete journal index"
                        ),
                    ),
                    patch.object(
                        store_module,
                        "_validated_transition_journal_chain",
                        side_effect=AssertionError(
                            "targeted retrieval scanned the journal chain"
                        ),
                    ),
                ):
                    (exact, first, second), work = _measure(
                        retrieve,
                        routine=True,
                    )
                self.assertIsNotNone(exact)
                self.assertEqual(exact["revision"], historical.revision)
                self.assertEqual(len(first["records"]), 2)
                self.assertEqual(len(second["records"]), 2)
                self.assertTrue(
                    set(
                        (item["source_family"], item["identity"], item["revision"])
                        for item in first["records"]
                    ).isdisjoint(
                        (item["source_family"], item["identity"], item["revision"])
                        for item in second["records"]
                    )
                )
                self.assertFalse(
                    any(
                        "SELECT * FROM transition_journal ORDER BY sequence_no"
                        in statement
                        for statement in work.statements
                    ),
                    work.statements,
                )
                self._assert_routine_plans(fixture, work)
                observations.append(work.summary())

        # The same exact record and two fixed-size pages perform the same
        # statement families as unrelated retained history grows.
        counts = [item["statements"] for item in observations]
        self.assertLessEqual(max(counts) - min(counts), 8, observations)

    def test_root_owner_head_seek_sql_microbenchmark_has_bounded_page_cost(self):
        """Measure only the unauthenticated head-table seek SQL primitive."""

        measurements = []
        with tempfile.TemporaryDirectory() as temporary:
            fixture = _HistoryFixture(
                Path(temporary) / "native-root-owner-pages",
                self.canonical,
            )
            inserted = 0

            def selected_owner(
                _connection,
                *,
                project_id,
                kind,
                identity,
                cut_project_commit,
            ):
                self.assertEqual(project_id, "project.rh")
                self.assertEqual(kind, "context")
                return {
                    "owner": {
                        "mission_id": "mission.1",
                        "reference": {
                            "kind": kind,
                            "identity": identity,
                            "revision": 1,
                            "payload_sha256": "a" * 64,
                        },
                        "document": {
                            "context_id": identity,
                            "mission_id": "mission.1",
                        },
                    },
                    "origin_project_commit": cut_project_commit,
                }

            for size in (100, 1_000, 10_000):
                with closing(sqlite3.connect(fixture.paths.database)) as connection:
                    connection.executemany(
                        "INSERT INTO context_head("
                        "object_id, revision, payload_digest, project_commit_no"
                        ") VALUES (?, 1, ?, ?)",
                        (
                            (
                                f"context.measure.{ordinal:05d}",
                                "a" * 64,
                                fixture.commit,
                            )
                            for ordinal in range(inserted, size)
                        ),
                    )
                    connection.commit()
                inserted = size

                for page_size in (1, 8, 25):
                    observed = []
                    page_steps = []
                    page_statements = []
                    statements = []
                    vm_steps = [0]

                    def progress():
                        vm_steps[0] += 1
                        return 0

                    with (
                        patch.object(
                            store_module,
                            "_root_owner_record_at_cut_from_connection",
                            side_effect=selected_owner,
                        ),
                        fixture.store.direct_recovery_read_scope(),
                    ):
                        horizon = fixture.store.read_root_retrieval_horizon(
                            cut_project_commit=fixture.commit
                        )
                        bound = fixture.store._direct_recovery_read_connection.get()
                        self.assertIsNotNone(bound)
                        assert bound is not None
                        bound.set_trace_callback(statements.append)
                        bound.set_progress_handler(progress, 1)
                        try:
                            after = None
                            while True:
                                before_steps = vm_steps[0]
                                before_statements = len(statements)
                                page = fixture.store.read_root_owner_page(
                                    kinds=("context",),
                                    cut_project_commit=fixture.commit,
                                    horizon=horizon,
                                    after=after,
                                    limit=page_size,
                                )
                                observed.extend(
                                    str(item["owner"]["reference"]["identity"])
                                    for item in page["records"]
                                )
                                page_steps.append(vm_steps[0] - before_steps)
                                page_statements.append(
                                    len(statements) - before_statements
                                )
                                after = page["next_after"]
                                if after is None:
                                    break
                        finally:
                            bound.set_progress_handler(None, 0)
                            bound.set_trace_callback(None)

                    self.assertEqual(len(observed), size)
                    self.assertEqual(len(set(observed)), size)
                    self.assertEqual(observed, sorted(observed))
                    expected_pages = (size + page_size - 1) // page_size
                    self.assertEqual(len(page_steps), expected_pages)
                    steady_steps = page_steps[1:-1]
                    if steady_steps:
                        self.assertLessEqual(
                            max(steady_steps),
                            min(steady_steps) * 1.10 + 25,
                            (size, page_size, page_steps[:3], page_steps[-3:]),
                        )
                    steady_statements = page_statements[1:-1]
                    if steady_statements:
                        self.assertEqual(
                            min(steady_statements),
                            max(steady_statements),
                            (size, page_size, page_statements[:3]),
                        )
                    measurements.append(
                        {
                            "records": size,
                            "page_size": page_size,
                            "pages": expected_pages,
                            "statements": len(statements),
                            "vm_steps": vm_steps[0],
                            "steady_page_vm_min": (
                                min(steady_steps) if steady_steps else page_steps[0]
                            ),
                            "steady_page_vm_max": (
                                max(steady_steps) if steady_steps else page_steps[0]
                            ),
                        }
                    )

        for page_size in (1, 8, 25):
            selected = [
                item for item in measurements if item["page_size"] == page_size
            ]
            for smaller, larger in zip(selected, selected[1:]):
                size_ratio = larger["records"] / smaller["records"]
                self.assertLessEqual(
                    larger["vm_steps"],
                    smaller["vm_steps"] * size_ratio * 1.35 + 500,
                    selected,
                )
                self.assertLessEqual(
                    larger["statements"],
                    smaller["statements"] * size_ratio + 10,
                    selected,
                )
        print("ROOT6_HEAD_SEEK_SQL_MICROBENCHMARK=" + json.dumps(measurements))

    def test_native_capture_coverage_work_is_fixed_by_first_page_head_horizon(self):
        """Post-cut linked owners cannot enlarge later Capture-page work."""

        observations = []
        with tempfile.TemporaryDirectory() as temporary:
            fixture = _HistoryFixture(
                Path(temporary) / "native-capture-horizon",
                self.canonical,
            )
            fixture.append_capture()
            capture_id = fixture.captures[-1]
            with fixture.store.direct_recovery_read_scope():
                horizon = fixture.store.read_root_retrieval_horizon(
                    cut_project_commit=fixture.commit
                )
            self.assertEqual(horizon["evidence"], 1)
            inserted = 0
            for size in (1_000, 10_000):
                with closing(sqlite3.connect(fixture.paths.database)) as connection:
                    connection.executemany(
                        "INSERT INTO evidence_item_head("
                        "evidence_id, revision, payload_digest, project_commit_no"
                        ") VALUES (?, 1, ?, ?)",
                        (
                            (f"evidence.post-cut.{ordinal:05d}", "b" * 64, fixture.commit)
                            for ordinal in range(inserted, size)
                        ),
                    )
                    connection.executemany(
                        "INSERT INTO evidence_capture_source("
                        "evidence_id, evidence_revision, source_ordinal, capture_id, "
                        "artifact_ordinal, exact_scope_json"
                        ") VALUES (?, 1, 0, ?, 0, '{}')",
                        (
                            (f"evidence.post-cut.{ordinal:05d}", capture_id)
                            for ordinal in range(inserted, size)
                        ),
                    )
                    connection.commit()
                inserted = size

                statements = []
                vm_steps = [0]

                def progress():
                    vm_steps[0] += 1
                    return 0

                with fixture.store.direct_recovery_read_scope():
                    bound = fixture.store._direct_recovery_read_connection.get()
                    self.assertIsNotNone(bound)
                    assert bound is not None
                    bound.set_trace_callback(statements.append)
                    bound.set_progress_handler(progress, 1)
                    try:
                        page = fixture.store.read_root_capture_page(
                            mission_id="mission.1",
                            cut_project_commit=fixture.commit,
                            horizon=horizon,
                            after=None,
                            limit=1,
                        )
                    finally:
                        bound.set_progress_handler(None, 0)
                        bound.set_trace_callback(None)
                self.assertEqual(len(page["records"]), 1)
                self.assertEqual(
                    str(page["records"][0]["capture_id"]),
                    capture_id,
                )
                self.assertTrue(page["records"][0]["artifacts"][0]["pending"])
                self.assertFalse(
                    any("FROM evidence_item_head" in statement for statement in statements),
                    statements,
                )
                self.assertTrue(
                    any(
                        "FROM capture_artifact_coverage_node WHERE node_digest ="
                        in statement
                        for statement in statements
                    ),
                    statements,
                )
                observations.append(
                    {
                        "post_horizon_linked_evidence": size,
                        "statements": len(statements),
                        "vm_steps": vm_steps[0],
                    }
                )
        self.assertEqual(
            observations[0]["statements"], observations[1]["statements"], observations
        )
        self.assertLessEqual(
            observations[1]["vm_steps"], observations[0]["vm_steps"] + 25, observations
        )
        print("ROOT6_CAPTURE_HORIZON_MEASUREMENTS=" + json.dumps(observations))

    def test_public_capture_pages_ignore_one_large_current_evidence_inventory(self):
        """The real facade exhausts N covered Captures without N-source rescans."""

        measurements = []
        with tempfile.TemporaryDirectory() as temporary:
            fixture = _HistoryFixture(
                Path(temporary) / "public-capture-coverage-scale",
                self.canonical,
            )
            original_validate = store_module._validated_transition_journal_row
            for size in (100, 300, 1_000):
                while len(fixture.captures) < size:
                    fixture.append_capture()
                evidence_outcome = fixture.cover_capture_prefix(size)
                evidence_commit = evidence_outcome.project_commit

                def reject_current_evidence_envelope(row, *args, **kwargs):
                    if (
                        "project_commit_no" in row.keys()
                        and row["project_commit_no"] == evidence_commit
                    ):
                        raise AssertionError(
                            "public Capture page reparsed the N-source Evidence journal"
                        )
                    return original_validate(row, *args, **kwargs)

                def exhaust():
                    cursor = None
                    observed = []
                    pages = 0
                    while True:
                        interface = MissionInterface(
                            store=fixture.store,
                            cas=EvidenceCAS(fixture.paths),
                            expected_mission_id="mission.1",
                            canonical_snapshot=self.canonical,
                        )
                        selection = {
                            "mode": "captures",
                            "purpose": "Verify exact bounded Capture coverage.",
                            "pending_state": "fully_covered",
                            "page_size": 1,
                        }
                        if cursor is not None:
                            selection["cursor"] = cursor
                        response = interface.execute_semantic_operation(
                            {
                                "schema_version": (
                                    MISSION_SEMANTIC_REQUEST_SCHEMA_VERSION
                                ),
                                "operation": RETRIEVE,
                                "input": selection,
                            },
                            executive_epoch_id=fixture.epoch_id,
                            root_query_context={"cursor_mac_key": "ab" * 32},
                        )
                        if response["status"] != "completed":
                            raise AssertionError(response)
                        page = response["result"]
                        observed.extend(page["items"])
                        pages += 1
                        cursor = page["next_cursor"]
                        if cursor is None:
                            return observed, pages

                forbidden = AssertionError(
                    "public Capture retrieval used an exhaustive coverage route"
                )
                with (
                    patch.object(
                        store_module,
                        "_validated_transition_journal_row",
                        side_effect=reject_current_evidence_envelope,
                    ),
                    patch.object(
                        store_module,
                        "_capture_coverage_current_entries",
                        side_effect=forbidden,
                    ),
                    patch.object(
                        store_module,
                        "_capture_coverage_evidence_scopes_from_connection",
                        side_effect=forbidden,
                    ),
                    patch.object(
                        store_module,
                        "_validated_journal_index",
                        side_effect=forbidden,
                    ),
                    patch.object(
                        store_module,
                        "_validated_transition_journal_chain",
                        side_effect=forbidden,
                    ),
                    patch.object(
                        WorkspaceStore,
                        "_verify_current_boundary",
                        side_effect=forbidden,
                    ),
                    patch.object(
                        WorkspaceStore,
                        "read_direct_recovery_facts",
                        side_effect=forbidden,
                    ),
                    patch.object(
                        fixture.store,
                        "read_raw_capture_artifact_bytes",
                        side_effect=forbidden,
                    ),
                ):
                    (items, pages), work = _measure(exhaust, routine=True)

                self.assertEqual(len(items), size)
                self.assertEqual(pages, size)
                self.assertEqual(
                    {item["id"] for item in items},
                    {f"capture:{capture_id}" for capture_id in fixture.captures[:size]},
                )
                self.assertTrue(
                    all(item["pending_state"] == "fully_covered" for item in items)
                )
                current_full_envelopes = [
                    statement
                    for statement in work.statements
                    if re.search(
                        rf"SELECT \* FROM transition_journal WHERE "
                        rf"project_commit_no = {evidence_commit}(?:\s|$)",
                        statement,
                        re.I,
                    )
                ]
                self.assertFalse(current_full_envelopes, current_full_envelopes)
                evidence_commit_reads = [
                    statement
                    for statement in work.statements
                    if "transition_journal" in statement.lower()
                    and re.search(
                        rf"project_commit_no\s*=\s*{evidence_commit}(?:\s|$|\))",
                        statement,
                        re.I,
                    )
                ]
                self.assertFalse(
                    any(
                        "auxiliary_writes_json" in statement.lower()
                        or "evidence_head_advances_json" in statement.lower()
                        for statement in evidence_commit_reads
                    ),
                    evidence_commit_reads,
                )
                self.assertFalse(
                    any(
                        "evidence_capture_source" in statement.lower()
                        for statement in work.statements
                    ),
                    work.statements,
                )
                coverage_queries = [
                    statement
                    for statement in work.statements
                    if "capture_artifact_coverage_node" in statement.lower()
                ]
                self.assertTrue(coverage_queries)
                self.assertTrue(
                    all("node_digest =" in statement for statement in coverage_queries),
                    coverage_queries[:20],
                )
                measurements.append(
                    {
                        "captures": size,
                        "pages": pages,
                        "statements": len(work.statements),
                        "vm_steps": work.vm_steps,
                        "coverage_queries": len(coverage_queries),
                    }
                )

        for smaller, larger in zip(measurements, measurements[1:]):
            nlogn_ratio = (
                larger["captures"] * larger["captures"].bit_length()
            ) / (
                smaller["captures"] * smaller["captures"].bit_length()
            )
            self.assertLessEqual(
                larger["statements"],
                smaller["statements"] * nlogn_ratio * 1.35 + 1_000,
                measurements,
            )
            self.assertLessEqual(
                larger["vm_steps"],
                smaller["vm_steps"] * nlogn_ratio * 1.50 + 10_000,
                measurements,
            )
        print(
            "ROOT6_PUBLIC_CAPTURE_COVERAGE_MEASUREMENTS="
            + json.dumps(measurements, sort_keys=True)
        )

    def test_capture_coverage_full_audit_is_linear_for_one_large_source_set(self):
        source_count = 512
        with tempfile.TemporaryDirectory() as temporary:
            fixture = _HistoryFixture(
                Path(temporary) / "coverage-full-audit-large-source-set",
                self.canonical,
            )
            fixture.append_capture(artifact_count=source_count)
            capture_id = fixture.captures[-1]
            record = prepare_evidence_meaning(
                authority=fixture.authority,
                evidence_id="evidence.root6.large-current-source-set",
                statement="Every exact artifact in one Capture was reviewed.",
                exact_scope="all exact artifacts in one synthetic Capture",
                strength="exact_finite_identity",
                semantic_role="result",
                authority_basis="direct review of every exact artifact",
                sources=tuple(
                    CaptureScope(
                        capture_id,
                        ordinal,
                        {"coverage": "complete artifact"},
                    )
                    for ordinal in range(source_count)
                ),
                non_inferences=("does not establish a mathematical theorem",),
            )
            commit_evidence_meaning(
                fixture.store,
                record=record,
                lease=fixture.lease,
                actor="root6-fixture",
            )
            fixture.commit = int(
                fixture.store.read_metadata()["current_project_commit"]
            )

            set_calls: list[dict[str, object]] = []
            original_set_count = store_module._CaptureCoverageMutation.set_count

            def counted_set_count(mutation, **kwargs):
                set_calls.append(dict(kwargs))
                return original_set_count(mutation, **kwargs)

            with (
                patch.object(
                    store_module,
                    "_verify_capture_coverage_projection_full",
                    wraps=store_module._verify_capture_coverage_projection_full,
                ) as coverage_audit,
                patch.object(
                    store_module,
                    "_declares_complete_capture_artifact_scope",
                    wraps=store_module._declares_complete_capture_artifact_scope,
                ) as classifications,
                patch.object(
                    store_module._CaptureCoverageMutation,
                    "set_count",
                    new=counted_set_count,
                ),
            ):
                report, work = _measure(
                    fixture.store.verify_integrity,
                    routine=False,
                )

            coverage_audit.assert_called_once()
            self.assertEqual(classifications.call_count, source_count)
            self.assertEqual(len(set_calls), 2 * source_count)
            self.assertEqual(report.current_project_commit, fixture.commit)
            for ordinal in range(source_count):
                sequence = [
                    (call["coverer_count"], call["require_absent"])
                    for call in set_calls
                    if call["artifact_ordinal"] == ordinal
                ]
                self.assertEqual(sequence, [(0, True), (1, False)])

            source_queries = [
                " ".join(statement.lower().split())
                for statement in work.statements
                if "evidence_capture_source" in statement.lower()
            ]
            unfiltered = [
                statement for statement in source_queries
                if " where " not in statement
            ]
            exact_reads = [
                statement for statement in source_queries
                if " where " in statement and "source_ordinal =" in statement
            ]
            revision_reads = [
                statement for statement in source_queries
                if " where " in statement and "source_ordinal =" not in statement
            ]
            self.assertEqual(len(unfiltered), 2, source_queries[:10])
            # The authoritative auxiliary-origin audit and the independent
            # root6 retained-row audit each authenticate every source once.
            # Capture-coverage replay itself is the single unfiltered pass
            # measured above; no source count is multiplied by another
            # retained-history dimension.
            self.assertEqual(
                len(exact_reads), 2 * source_count, source_queries[:10]
            )
            self.assertEqual(len(revision_reads), 2, revision_reads)
            self.assertEqual(
                len(source_queries),
                len(unfiltered) + len(exact_reads) + len(revision_reads),
            )

    def test_capture_coverage_storage_grows_only_with_changed_paths(self):
        with tempfile.TemporaryDirectory() as temporary:
            fixture = _HistoryFixture(
                Path(temporary) / "coverage-storage-growth",
                self.canonical,
            )
            while len(fixture.captures) < 64:
                fixture.append_capture()

            def storage_state():
                with closing(sqlite3.connect(fixture.paths.database)) as connection:
                    connection.row_factory = sqlite3.Row
                    metadata = connection.execute(
                        "SELECT current_project_commit FROM workspace_metadata "
                        "WHERE singleton = 1"
                    ).fetchone()
                    assert metadata is not None
                    journal = connection.execute(
                        "SELECT capture_coverage_root_digest, "
                        "capture_coverage_entry_count FROM transition_journal "
                        "WHERE project_commit_no = ?",
                        (int(metadata["current_project_commit"]),),
                    ).fetchone()
                    assert journal is not None
                    return (
                        str(journal["capture_coverage_root_digest"]),
                        int(journal["capture_coverage_entry_count"]),
                        int(
                            connection.execute(
                                "SELECT COUNT(*) FROM "
                                "capture_artifact_coverage_node"
                            ).fetchone()[0]
                        ),
                    )

            initial = storage_state()
            self.assertEqual(initial[1], 64)
            fixture.append_branch()
            after_unrelated = storage_state()
            self.assertEqual(after_unrelated, initial)

            fixture.cover_capture_prefix(1)
            after_one_change = storage_state()
            self.assertEqual(after_one_change[1], 64)
            self.assertNotEqual(after_one_change[0], initial[0])
            self.assertGreater(after_one_change[2], initial[2])
            logarithmic_path_budget = 8 * after_one_change[1].bit_length()
            self.assertLessEqual(
                after_one_change[2] - initial[2],
                logarithmic_path_budget,
            )

            prior = fixture.covering_evidence
            assert prior is not None
            unchanged = prepare_evidence_meaning(
                authority=fixture.authority,
                evidence_id=prior.evidence_id,
                statement="The same exact Capture artifact remains reviewed.",
                exact_scope="the same one exact Capture artifact",
                strength="exact_finite_identity",
                semantic_role="result",
                authority_basis="direct review of exact captured material",
                sources=(
                    CaptureScope(
                        fixture.captures[0],
                        0,
                        {"coverage": "complete artifact"},
                    ),
                ),
                non_inferences=("does not establish a mathematical theorem",),
                expected_head_revision=prior.revision,
                expected_head_payload_digest=prior.payload_digest,
            )
            commit_evidence_meaning(
                fixture.store,
                record=unchanged,
                lease=fixture.lease,
                actor="root6-fixture",
            )
            after_noop_delta = storage_state()
            self.assertEqual(after_noop_delta, after_one_change)

            unchanged_read = read_evidence_meaning(
                fixture.store,
                evidence_id=unchanged.evidence_id,
            )
            moved = prepare_evidence_meaning(
                authority=fixture.authority,
                evidence_id=unchanged.evidence_id,
                statement="A different exact Capture artifact was reviewed.",
                exact_scope="one different exact Capture artifact",
                strength="exact_finite_identity",
                semantic_role="result",
                authority_basis="direct review of exact captured material",
                sources=(
                    CaptureScope(
                        fixture.captures[1],
                        0,
                        {"coverage": "complete artifact"},
                    ),
                ),
                non_inferences=("does not establish a mathematical theorem",),
                expected_head_revision=unchanged_read.revision,
                expected_head_payload_digest=unchanged_read.payload_digest,
            )
            commit_evidence_meaning(
                fixture.store,
                record=moved,
                lease=fixture.lease,
                actor="root6-fixture",
            )
            after_two_changes = storage_state()
            self.assertEqual(after_two_changes[1], 64)
            self.assertNotEqual(after_two_changes[0], after_noop_delta[0])
            self.assertGreater(after_two_changes[2], after_noop_delta[2])
            self.assertLessEqual(
                after_two_changes[2] - after_noop_delta[2],
                16 * after_two_changes[1].bit_length(),
            )
            fixture.store.verify_integrity()

    def test_seek_page_preserves_legacy_serialized_order_and_uses_expression_index(self):
        with tempfile.TemporaryDirectory() as temporary:
            fixture = _HistoryFixture(Path(temporary) / "workspace", self.canonical)
            first = fixture.append_named_branch("branch.order")
            fixture.append_named_branch("branch.order.more")
            for _ in range(9):
                fixture.advance_branch(first.changed_heads[0].object_id.value)
            fixture.append_capture(artifact_count=12)
            head = fixture.store.get_head(
                TypedWorkspaceId(IdentityKind.BRANCH, "branch.order")
            )
            self.assertIsNotNone(head)
            assert head is not None
            self.assertEqual(head.reference.revision, 10)
            self.assertEqual(
                fixture.commit,
                int(fixture.store.read_metadata()["current_project_commit"]),
            )

            with fixture.store.direct_recovery_read_scope():
                for invalid_cut in (True, 1.5, "1", -1):
                    with self.subTest(invalid_cut=invalid_cut):
                        with self.assertRaises(ValueError):
                            fixture.store.read_retained_history_page(
                                source_families=("branches",),
                                cut_project_commit=invalid_cut,
                                after=None,
                                limit=1,
                            )

            records = []
            after = None
            with fixture.store.direct_recovery_read_scope():
                while True:
                    page = fixture.store.read_retained_history_page(
                        source_families=("branches",),
                        cut_project_commit=fixture.commit,
                        after=after,
                        limit=1,
                    )
                    records.extend(page["records"])
                    after = page["next_after"]
                    if after is None:
                        break

            serialized = [
                f"{item['identity']}@{item['revision']}" for item in records
            ]
            self.assertEqual(serialized, sorted(serialized))
            self.assertLess(
                serialized.index("branch.order.more@1"),
                serialized.index("branch.order@1"),
            )
            self.assertIn("branch.order@10", serialized, serialized)
            self.assertIn("branch.order@2", serialized, serialized)
            self.assertLess(
                serialized.index("branch.order@10"),
                serialized.index("branch.order@2"),
            )
            self.assertEqual(len(serialized), len(set(serialized)))

            with closing(sqlite3.connect(fixture.paths.database)) as connection:
                plans = tuple(
                    str(row[3])
                    for row in connection.execute(
                        "EXPLAIN QUERY PLAN "
                        "SELECT object_id, revision FROM branch_revision "
                        "WHERE object_id || '@' || CAST(revision AS TEXT) > ? "
                        "ORDER BY object_id || '@' || CAST(revision AS TEXT) LIMIT ?",
                        ("branch.order@1", 32),
                    )
                )
            self.assertTrue(
                any("branch_revision_history_seek" in plan for plan in plans),
                plans,
            )

            artifact_records = []
            after = None
            with fixture.store.direct_recovery_read_scope():
                while True:
                    page = fixture.store.read_retained_history_page(
                        source_families=("capture_artifacts",),
                        cut_project_commit=fixture.commit,
                        after=after,
                        limit=1,
                    )
                    artifact_records.extend(page["records"])
                    after = page["next_after"]
                    if after is None:
                        break
            artifact_ids = [str(item["identity"]) for item in artifact_records]
            self.assertEqual(artifact_ids, sorted(artifact_ids))
            capture_id = fixture.captures[-1]
            self.assertLess(
                artifact_ids.index(f"{capture_id}#10"),
                artifact_ids.index(f"{capture_id}#2"),
            )
            with closing(sqlite3.connect(fixture.paths.database)) as connection:
                artifact_plans = tuple(
                    str(row[3])
                    for row in connection.execute(
                        "EXPLAIN QUERY PLAN "
                        "SELECT capture_id, ordinal FROM raw_capture_artifact "
                        "WHERE capture_id || '#' || CAST(ordinal AS TEXT) > ? "
                        "ORDER BY capture_id || '#' || CAST(ordinal AS TEXT) LIMIT ?",
                        (f"{capture_id}#1", 32),
                    )
                )
            self.assertTrue(
                any("raw_capture_artifact_history_seek" in plan for plan in artifact_plans),
                artifact_plans,
            )

    def test_capture_history_page_collects_rows_artifacts_and_origins_as_sets(self):
        with tempfile.TemporaryDirectory() as temporary:
            fixture = _HistoryFixture(
                Path(temporary) / "capture-page",
                self.canonical,
            )
            for _ in range(6):
                fixture.append_capture(artifact_count=2)

            def retrieve():
                with fixture.store.direct_recovery_read_scope():
                    return fixture.store.read_retained_history_page(
                        source_families=("captures",),
                        cut_project_commit=fixture.commit,
                        after=None,
                        limit=4,
                    )

            page, work = _measure(retrieve, routine=True)
            self.assertEqual(len(page["records"]), 4)
            self.assertIsNotNone(page["next_after"])
            statements = [statement.casefold() for statement in work.statements]
            self.assertEqual(
                sum(
                    "select * from raw_capture where capture_id in (" in statement
                    for statement in statements
                ),
                1,
                work.statements,
            )
            self.assertEqual(
                sum(
                    "select * from raw_capture_artifact where capture_id in ("
                    in statement
                    for statement in statements
                ),
                1,
                work.statements,
            )
            self.assertEqual(
                sum(
                    "as capture_id, project_commit_no from transition_journal"
                    in statement
                    and " in (" in statement
                    for statement in statements
                ),
                1,
                work.statements,
            )
            self.assertFalse(
                any(
                    "select * from raw_capture where capture_id =" in statement
                    for statement in statements
                ),
                work.statements,
            )

    def test_equal_delta_open_write_reissue_and_full_audit_at_three_history_sizes(self):
        """The 1000-commit probe is one sequential fixture, never a production root."""
        result = getattr(getattr(self, "_outcome", None), "result", None)
        fixture_progress = getattr(result, "fixture_progress", None)

        def report(phase, completed):
            # Reuse the staging runner's optional fixed-phase hook. Ordinary
            # unittest has no hook; reporting never enters a measured operation.
            if fixture_progress is not None:
                fixture_progress(self, phase, completed, 1)

        observations = []
        with tempfile.TemporaryDirectory() as temporary:
            fixture = _HistoryFixture(Path(temporary) / "workspace", self.canonical)
            interface = MissionInterface(
                store=fixture.store,
                cas=EvidenceCAS(fixture.paths),
                expected_mission_id="mission.1",
                canonical_snapshot=self.canonical,
            )
            self.assertEqual(
                (fixture.store.read_metadata()["schema_version"], fixture.store.read_metadata()["root_digest_version"]),
                # This native fixture exercises current schema12/root6;
                # retained schema10 compatibility belongs to migration tests.
                (12, 6),
            )
            for rung, size in enumerate((16, 128, 1000)):
                phase = 4 * rung
                report(phase, 0)
                fixture.grow_to(size)
                report(phase, 1)
                report(phase + 1, 0)
                _, opened = _measure(lambda: WorkspaceStore.open(fixture.paths), routine=True)
                opened_interface, interface_opened = _measure(
                    lambda: MissionInterface.open(
                        fixture.paths.root,
                        expected_project_id="project.rh",
                        expected_mission_id="mission.1",
                        canonical_snapshot=self.canonical,
                    ),
                    routine=True,
                )
                self.assertEqual(opened_interface._mission_id, "mission.1")
                _, reissued = _measure(
                    lambda: fixture.store.reissue_writer_lease(fixture.principal), routine=True
                )
                outcome, written = _measure(fixture.append_branch, routine=True)
                self.assertEqual(len(outcome.changed_heads), 1)
                _, capture_written = _measure(fixture.append_capture, routine=True)
                _, capture_opened = _measure(lambda: WorkspaceStore.open(fixture.paths), routine=True)
                snapshot, host_snapshot = _measure(
                    interface.host_snapshot, routine=True
                )
                self.assertEqual(
                    snapshot["current_state"]["observed_project_commit"], size + 2
                )
                self.assertEqual(
                    tuple(
                        item["candidate_ref"]["identity"]
                        for item in snapshot["current_state"]["open_candidate_a1"]
                    ),
                    ("candidate.root6.current-a1",),
                )
                report(phase + 1, 1)
                report(phase + 2, 0)
                # This complete audit consumes only aggregate counts below;
                # retain SQL text for the routine query-plan checks, not here.
                audited, audit = _measure(
                    fixture.store.verify_integrity, routine=False, retain_statements=False,
                )
                self.assertEqual(audited.current_project_commit, size + 2)
                report(phase + 2, 1)
                report(phase + 3, 0)
                for work in (
                    opened,
                    interface_opened,
                    reissued,
                    written,
                    capture_written,
                    capture_opened,
                    host_snapshot,
                ):
                    self._assert_routine_plans(fixture, work)
                counts = {}
                with closing(sqlite3.connect(fixture.paths.database)) as connection:
                    for table in ("project_commit", "transition_journal", "branch_revision", "branch_head", "raw_capture", "raw_capture_artifact"):
                        counts[table] = connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
                observations.append({
                    "retained_project_commits_before_write": size,
                    "rows_after_write": counts,
                    "open": opened.summary(),
                    "interface_open": interface_opened.summary(),
                    "reissue": reissued.summary(),
                    "write": written.summary(),
                    "capture_write": capture_written.summary(),
                    "capture_open": capture_opened.summary(),
                    "host_snapshot": host_snapshot.summary(),
                    "audit": audit.summary(),
                })
                observations[-1]["capture_write"]["coverage_node_reads"] = sum(
                    "FROM capture_artifact_coverage_node WHERE node_digest ="
                    in statement
                    for statement in capture_written.statements
                )
                self.assertFalse(
                    any(
                        "FROM evidence_item_head AS head NOT INDEXED" in statement
                        for statement in capture_written.statements
                    ),
                    capture_written.statements,
                )
                report(phase + 3, 1)

            report(12, 0)
            plans = {}
            with closing(sqlite3.connect(fixture.paths.database)) as connection:
                for table in ("transition_journal", "command_result"):
                    index_name = f"{table}_project_commit"
                    self.assertEqual(
                        tuple(row[2] for row in connection.execute(f"PRAGMA index_info({index_name})")),
                        ("project_commit_no",),
                    )
                    plans[table] = [str(row[3]) for row in connection.execute(
                        f"EXPLAIN QUERY PLAN SELECT * FROM {table} WHERE project_commit_no = ?", (500,)
                    )]
                    self.assertTrue(any("SEARCH " in plan and index_name in plan for plan in plans[table]), plans)
                    self.assertFalse(any("SCAN " in plan for plan in plans[table]), plans)
            report(12, 1)

        print("ROOT6_ASYMPTOTIC_EVIDENCE=" + json.dumps({
            "fixture": "supported_branch_and_raw_capture_commands_one_sequential_store",
            "measurements": observations,
            "exact_project_commit_query_plans": plans,
        }, sort_keys=True))
        for operation in (
            "open",
            "interface_open",
            "reissue",
            "write",
            "capture_open",
            "host_snapshot",
        ):
            statements = [item[operation]["statements"] for item in observations]
            steps = [item[operation]["vm_steps_100_quantum"] for item in observations]
            self.assertEqual(min(statements), max(statements), (operation, observations))
            # Progress callbacks round each connection down by <100 VM steps;
            # B-tree lookup depth can change without consuming history-sized rows.
            self.assertLessEqual(max(steps), min(steps) * 1.15 + 1000, (operation, observations))
        capture_statements = [
            item["capture_write"]["statements"] for item in observations
        ]
        capture_steps = [
            item["capture_write"]["vm_steps_100_quantum"]
            for item in observations
        ]
        capture_reads = [
            item["capture_write"]["coverage_node_reads"] for item in observations
        ]
        # One new artifact path-copies a balanced AVL route.  The exact work
        # may grow with tree height, never with the retained history itself.
        height_growth = (1_000).bit_length() - (16).bit_length()
        self.assertLessEqual(
            max(capture_statements) - min(capture_statements),
            6 * height_growth,
            observations,
        )
        self.assertLessEqual(
            max(capture_reads) - min(capture_reads),
            6 * height_growth,
            observations,
        )
        self.assertLessEqual(
            max(capture_steps), min(capture_steps) * 1.15 + 1_500,
            observations,
        )
        for metric in ("statements", "vm_steps_100_quantum"):
            first, middle, last = (item["audit"][metric] for item in observations)
            early_slope = (middle - first) / (128 - 16)
            late_slope = (last - middle) / (1000 - 128)
            self.assertGreater(early_slope, 0, (metric, observations))
            self.assertLessEqual(late_slope, early_slope * 1.75, (metric, observations))


class Root6TamperBoundaryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        loaded = load_canonical_snapshot(REPO_ROOT, source_commit="a" * 40)
        if not loaded.ok or loaded.value is None:
            raise AssertionError(loaded.failure)
        cls.canonical = loaded.value

    def _fixture(self, root: Path):
        fixture = _HistoryFixture(root, self.canonical)
        fixture.grow_to(10)
        return fixture

    @staticmethod
    def _tamper(fixture: _HistoryFixture, sql: str, parameters=()):
        with closing(sqlite3.connect(fixture.paths.database)) as connection:
            connection.execute(sql, parameters)
            connection.commit()

    def test_native_root6_commit_zero_rejects_forged_schema10_transition(self):
        with tempfile.TemporaryDirectory() as temporary:
            paths = WorkspacePaths.from_root(
                Path(temporary) / "native-commit-zero"
            )
            store = WorkspaceStore.initialize_direct_mission_workspace(
                paths,
                project_id="project.rh",
                canonical_snapshot=self.canonical,
                actor="root6-fixture",
                clock=_CLOCK,
            )
            paths = store.paths
            before = store.read_metadata()
            self.assertEqual(before["current_project_commit"], 0)
            self.assertIsNone(before["transition_head_digest"])

            with closing(
                store_module._connect_workspace(store.paths.database)
            ) as connection:
                contract = store_module.verify_schema_contract(
                    connection, schema_version=10
                )
                # Deliberately bypass the FK only to create a transition whose
                # missing migration witness must be caught by routine route
                # provenance validation rather than a complete FK scan.
                connection.execute("PRAGMA foreign_keys = OFF")
                execution_id = "migration.execution.forged-commit-zero"
                attempt_id = "migration.attempt.forged-commit-zero"
                values = {
                    "transition_id": (
                        f"workspace-root-contract.{execution_id}"
                    ),
                    "command_id": (
                        f"workspace-schema-root-transition.{attempt_id}"
                    ),
                    "writer_epoch": 1,
                    "migration_execution_id": execution_id,
                    "migration_attempt_id": attempt_id,
                    "plan_sha256": "1" * 64,
                    "verified_backup_manifest_sha256": "2" * 64,
                    "source_project_commit": 0,
                    "source_root_digest": before["current_root_digest"],
                    "source_transition_head_digest": None,
                    "canonical_authority_digest": (
                        before["canonical_authority_digest"]
                    ),
                    "source_schema_version": 9,
                    "target_schema_version": 10,
                    "source_root_digest_version": 5,
                    "target_root_digest_version": 6,
                    "target_migration_set_digest": contract.migration_digest,
                    "target_schema_object_digest": contract.schema_object_digest,
                    "canonical_effect": "none",
                    "created_at": _CLOCK(),
                }
                row_digest = store_module._row_digest(
                    "workspace_root_contract_transition", values
                )
                connection.execute(
                    "INSERT INTO workspace_root_contract_transition("
                    + ", ".join((*values, "row_digest"))
                    + ") VALUES ("
                    + ", ".join("?" for _ in range(len(values) + 1))
                    + ")",
                    (*values.values(), row_digest),
                )
                connection.commit()

            with patch.object(
                WorkspaceStore,
                "verify_integrity",
                side_effect=AssertionError(
                    "native routine open entered full audit"
                ),
            ):
                with self.assertRaisesRegex(
                    WorkspaceIntegrityError, "migration witness"
                ):
                    WorkspaceStore.open(
                        store.paths, expected_project_id="project.rh"
                    )

            with closing(sqlite3.connect(store.paths.database)) as connection:
                self.assertEqual(
                    connection.execute(
                        "SELECT current_project_commit FROM workspace_metadata "
                        "WHERE singleton = 1"
                    ).fetchone()[0],
                    0,
                )
                self.assertEqual(
                    connection.execute(
                        "SELECT COUNT(*) FROM transition_journal"
                    ).fetchone()[0],
                    0,
                )
                self.assertEqual(
                    connection.execute(
                        "SELECT COUNT(*) FROM command_result"
                    ).fetchone()[0],
                    0,
                )

    def test_native_root6_route_rejects_orphan_schema10_execution_at_any_cut(self):
        with tempfile.TemporaryDirectory() as temporary:
            for name, expected_message in (
                ("genesis", "orphaned schema10 migration witness"),
                (
                    "later",
                    "native root6 command envelope has orphaned migration state",
                ),
            ):
                with self.subTest(generation=name):
                    root = Path(temporary) / name
                    if name == "genesis":
                        paths = WorkspacePaths.from_root(root)
                        store = WorkspaceStore.initialize_direct_mission_workspace(
                            paths,
                            project_id="project.rh",
                            canonical_snapshot=self.canonical,
                            actor="root6-fixture",
                            clock=_CLOCK,
                        )
                    else:
                        store = self._fixture(root).store
                    with closing(sqlite3.connect(store.paths.database)) as connection:
                        connection.execute(
                            "INSERT INTO migration_execution("
                            "execution_id, attempt_id, source_schema_version, "
                            "target_schema_version, verified_backup_manifest_sha256, "
                            "applied_history_sha256, plan_sha256, started_at, "
                            "completed_at, status, canonical_effect) "
                            "VALUES (?, ?, 9, 10, ?, ?, ?, ?, ?, 'applied', 'none')",
                            (
                                f"migration.execution.orphan-{name}",
                                f"migration.attempt.orphan-{name}",
                                "1" * 64,
                                "2" * 64,
                                "3" * 64,
                                _CLOCK(),
                                _CLOCK(),
                            ),
                        )
                        connection.commit()
                    with patch.object(
                        WorkspaceStore,
                        "verify_integrity",
                        side_effect=AssertionError(
                            "native routine open entered full audit"
                        ),
                    ):
                        with self.assertRaisesRegex(
                            WorkspaceIntegrityError,
                            expected_message,
                        ):
                            WorkspaceStore.open(
                                store.paths,
                                expected_project_id="project.rh",
                            )

    def test_native_root6_genesis_rejects_every_command_result(self):
        with tempfile.TemporaryDirectory() as temporary:
            for project_commit_no in (0, 1):
                with self.subTest(project_commit_no=project_commit_no):
                    paths = WorkspacePaths.from_root(
                        Path(temporary) / f"result-at-{project_commit_no}"
                    )
                    store = WorkspaceStore.initialize_direct_mission_workspace(
                        paths,
                        project_id="project.rh",
                        canonical_snapshot=self.canonical,
                        actor="root6-fixture",
                        clock=_CLOCK,
                    )
                    paths = store.paths
                    result = canonical_payload(
                        {"result": "forged genesis result"}
                    )
                    with closing(sqlite3.connect(paths.database)) as connection:
                        connection.execute("PRAGMA foreign_keys = OFF")
                        connection.execute(
                            "INSERT INTO command_result("
                            "command_id, actor, request_digest, writer_epoch, "
                            "result_digest, result_json, project_commit_no, "
                            "created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                            (
                                f"command.forged-result-{project_commit_no}",
                                "root6-fixture",
                                "1" * 64,
                                1,
                                result.sha256,
                                result.text,
                                project_commit_no,
                                _CLOCK(),
                            ),
                        )
                        connection.commit()
                    with patch.object(
                        WorkspaceStore,
                        "verify_integrity",
                        side_effect=AssertionError(
                            "native routine open entered full audit"
                        ),
                    ):
                        with self.assertRaisesRegex(
                            WorkspaceIntegrityError,
                            "genesis commit must be command-free",
                        ):
                            WorkspaceStore.open(
                                paths, expected_project_id="project.rh"
                            )

    def test_native_root6_genesis_rejects_every_typed_owner_head(self):
        with tempfile.TemporaryDirectory() as temporary:
            for kind, (revision_table, head_table) in (
                store_module._REVISION_TABLES.items()
            ):
                with self.subTest(kind=kind.value):
                    paths = WorkspacePaths.from_root(
                        Path(temporary) / kind.value
                    )
                    store = WorkspaceStore.initialize_direct_mission_workspace(
                        paths,
                        project_id="project.rh",
                        canonical_snapshot=self.canonical,
                        actor="root6-fixture",
                        clock=_CLOCK,
                    )
                    paths = store.paths
                    object_id = f"{kind.value}.forged-genesis"
                    payload = canonical_payload(
                        {"kind": kind.value, "id": object_id}
                    )
                    values = {
                        "object_id": object_id,
                        "revision": 1,
                        "payload_json": payload.text,
                        "payload_digest": payload.sha256,
                        "predecessor_revision": None,
                        "created_actor": "root6-fixture",
                        "created_session_id": None,
                        "created_evidence_id": None,
                        "authorization_json": "{}",
                        "terminal_history_json": "[]",
                        "created_at": _CLOCK(),
                    }
                    with closing(sqlite3.connect(paths.database)) as connection:
                        connection.execute(
                            f"INSERT INTO {revision_table}("
                            + ", ".join((*values, "row_digest"))
                            + ") VALUES ("
                            + ", ".join("?" for _ in range(len(values) + 1))
                            + ")",
                            (
                                *values.values(),
                                store_module._row_digest(
                                    revision_table, values
                                ),
                            ),
                        )
                        connection.execute(
                            f"INSERT INTO {head_table}("
                            "object_id, revision, payload_digest, project_commit_no) "
                            "VALUES (?, 1, ?, 0)",
                            (object_id, payload.sha256),
                        )
                        connection.commit()
                    with patch.object(
                        WorkspaceStore,
                        "verify_integrity",
                        side_effect=AssertionError(
                            "native routine open entered full audit"
                        ),
                    ):
                        with self.assertRaisesRegex(
                            WorkspaceIntegrityError,
                            "fresh root6 genesis has a current owner head",
                        ):
                            WorkspaceStore.open(
                                paths, expected_project_id="project.rh"
                            )

    def test_native_root6_genesis_rejects_auxiliary_owner_heads(self):
        with tempfile.TemporaryDirectory() as temporary:
            for family, head_table, identity_column in (
                ("evidence", "evidence_item_head", "evidence_id"),
                (
                    "capture-annotation",
                    "capture_scope_annotation_head",
                    "annotation_id",
                ),
            ):
                with self.subTest(family=family):
                    paths = WorkspacePaths.from_root(Path(temporary) / family)
                    store = WorkspaceStore.initialize_direct_mission_workspace(
                        paths,
                        project_id="project.rh",
                        canonical_snapshot=self.canonical,
                        actor="root6-fixture",
                        clock=_CLOCK,
                    )
                    paths = store.paths
                    with closing(sqlite3.connect(paths.database)) as connection:
                        connection.execute("PRAGMA foreign_keys = OFF")
                        connection.execute(
                            f"INSERT INTO {head_table}("
                            f"{identity_column}, revision, payload_digest, "
                            "project_commit_no) VALUES (?, 1, ?, 0)",
                            (f"{family}.forged-genesis", "1" * 64),
                        )
                        connection.commit()
                    with patch.object(
                        WorkspaceStore,
                        "verify_integrity",
                        side_effect=AssertionError(
                            "native routine open entered full audit"
                        ),
                    ):
                        with self.assertRaisesRegex(
                            WorkspaceIntegrityError,
                            "fresh root6 genesis has a current owner head",
                        ):
                            WorkspaceStore.open(
                                paths, expected_project_id="project.rh"
                            )

    def test_routine_open_rejects_corruption_of_the_current_envelope(self):
        changes = {
            "metadata": "UPDATE workspace_metadata SET current_root_digest = ?",
            "current_commit": "UPDATE project_commit SET root_digest = ? WHERE commit_no = 10",
            "previous_root": "UPDATE project_commit SET root_digest = ? WHERE commit_no = 9",
            "current_transition": "UPDATE transition_journal SET actor = ? WHERE project_commit_no = 10",
            "transition_predecessor": "UPDATE transition_journal SET predecessor_digest = ? WHERE project_commit_no = 10",
            "current_result": "UPDATE command_result SET result_json = ? WHERE project_commit_no = 10",
            "current_writer": "UPDATE writer_epoch SET creation_basis = ? WHERE epoch = 1",
            "current_revision": "UPDATE branch_revision SET payload_json = ? WHERE object_id = 'branch.root6.000010'",
            "current_head": "UPDATE branch_head SET payload_digest = ? WHERE object_id = 'branch.root6.000010'",
        }
        with tempfile.TemporaryDirectory() as temporary:
            for name, sql in changes.items():
                with self.subTest(corruption=name):
                    fixture = self._fixture(Path(temporary) / name)
                    self._tamper(fixture, sql, ("0" * 64,))
                    with self.assertRaises(WorkspaceIntegrityError):
                        WorkspaceStore.open(fixture.paths)

    def test_root6_rejects_coordinated_semantic_result_tampering(self):
        """A recomputed replay-envelope self-hash cannot replace owner meaning."""

        with tempfile.TemporaryDirectory() as temporary:
            for verification, commit_no in (
                ("routine_open", 10),
                ("full_audit", 2),
                ("historical_replay", 2),
                ("historical_replay_root_mismatch", 2),
            ):
                with self.subTest(verification=verification):
                    fixture = self._fixture(Path(temporary) / verification)
                    with closing(sqlite3.connect(fixture.paths.database)) as connection:
                        connection.row_factory = sqlite3.Row
                        result_row = connection.execute(
                            "SELECT r.command_id, r.actor, r.request_digest, "
                            "r.result_json, j.result_payload_digest "
                            "FROM command_result r JOIN transition_journal j "
                            "ON j.project_commit_no = r.project_commit_no "
                            "WHERE r.project_commit_no = ?",
                            (commit_no,),
                        ).fetchone()
                        self.assertIsNotNone(result_row)
                        assert result_row is not None
                        document = json.loads(str(result_row["result_json"]))
                        self.assertEqual(
                            result_row["result_payload_digest"],
                            canonical_payload(document["result"]).sha256,
                        )
                        document["result"] = {"tampered": True}
                        if verification == "historical_replay_root_mismatch":
                            journal = connection.execute(
                                "SELECT * FROM transition_journal "
                                "WHERE project_commit_no = ?",
                                (commit_no,),
                            ).fetchone()
                            self.assertIsNotNone(journal)
                            assert journal is not None
                            semantic_result_digest = canonical_payload(
                                document["result"]
                            ).sha256
                            journal_body = {
                                "sequence_no": int(journal["sequence_no"]),
                                "project_id": str(journal["project_id"]),
                                "project_commit_no": int(
                                    journal["project_commit_no"]
                                ),
                                "command_id": str(journal["command_id"]),
                                "command_kind": str(journal["command_kind"]),
                                "request_digest": str(journal["request_digest"]),
                                "actor": str(journal["actor"]),
                                "writer_epoch": int(journal["writer_epoch"]),
                                "changed_heads": json.loads(
                                    str(journal["changed_heads_json"])
                                ),
                                "auxiliary_writes_digest": str(
                                    journal["auxiliary_writes_digest"]
                                ),
                                "evidence_head_advances_digest": str(
                                    journal["evidence_head_advances_digest"]
                                ),
                                "authorization_digest": canonical_payload(
                                    json.loads(str(journal["authorization_json"]))
                                ).sha256,
                                "canonical_effect": str(
                                    journal["canonical_effect"]
                                ),
                                "predecessor_digest": journal[
                                    "predecessor_digest"
                                ],
                                "created_at": str(journal["created_at"]),
                                "changed_head_rows_digest": canonical_payload(
                                    json.loads(
                                        str(journal["changed_head_rows_json"])
                                    )
                                ).sha256,
                                "result_payload_digest": semantic_result_digest,
                                "current_dependency_root_digest": str(
                                    journal["current_dependency_root_digest"]
                                ),
                                "current_dependency_entry_count": int(
                                    journal["current_dependency_entry_count"]
                                ),
                                "capture_coverage_root_digest": str(
                                    journal["capture_coverage_root_digest"]
                                ),
                                "capture_coverage_entry_count": int(
                                    journal["capture_coverage_entry_count"]
                                ),
                            }
                            rewritten_transition_digest = hashlib.sha256(
                                store_module.canonical_json_bytes(journal_body)
                            ).hexdigest()
                            document["transition_digest"] = (
                                rewritten_transition_digest
                            )
                            connection.execute(
                                "UPDATE transition_journal SET "
                                "result_payload_digest = ?, digest_sha256 = ? "
                                "WHERE project_commit_no = ?",
                                (
                                    semantic_result_digest,
                                    rewritten_transition_digest,
                                    commit_no,
                                ),
                            )
                            connection.execute(
                                "UPDATE project_commit SET "
                                "transition_head_digest = ? WHERE commit_no = ?",
                                (rewritten_transition_digest, commit_no),
                            )
                        rewritten = canonical_payload(document)
                        connection.execute(
                            "UPDATE command_result SET result_json = ?, "
                            "result_digest = ? WHERE project_commit_no = ?",
                            (rewritten.text, rewritten.sha256, commit_no),
                        )
                        connection.commit()

                    if verification == "routine_open":
                        with self.assertRaisesRegex(
                            WorkspaceIntegrityError,
                            "semantic-result|command result",
                        ):
                            WorkspaceStore.open(fixture.paths)
                    else:
                        if verification == "full_audit":
                            with self.assertRaisesRegex(
                                WorkspaceIntegrityError,
                                "command result envelope",
                            ):
                                fixture.store.verify_integrity()
                        else:
                            with fixture.store._connection() as connection:
                                with self.assertRaisesRegex(
                                    WorkspaceIntegrityError,
                                    (
                                        "root6 commitment|command result"
                                        if verification
                                        == "historical_replay_root_mismatch"
                                        else "command result"
                                    ),
                                ):
                                    fixture.store._replay_if_present(
                                        connection,
                                        command_id=str(result_row["command_id"]),
                                        actor=str(result_row["actor"]),
                                        request_digest=str(
                                            result_row["request_digest"]
                                        ),
                                    )

    def test_routine_open_rejects_current_delta_auxiliary_corruption(self):
        with tempfile.TemporaryDirectory() as temporary:
            for table, column in (("raw_capture", "provenance_json"), ("raw_capture_artifact", "logical_name")):
                with self.subTest(table=table):
                    fixture = self._fixture(Path(temporary) / table)
                    fixture.append_capture()
                    self._tamper(
                        fixture,
                        f"UPDATE {table} SET {column} = ? WHERE capture_id = ?",
                        ("tampered", fixture.captures[-1]),
                    )
                    with self.assertRaises(WorkspaceIntegrityError):
                        WorkspaceStore.open(fixture.paths)

    def test_capture_coverage_journal_fields_are_physically_authenticated(self):
        with tempfile.TemporaryDirectory() as temporary:
            for field in (
                "capture_coverage_root_digest",
                "capture_coverage_entry_count",
            ):
                with self.subTest(field=field):
                    fixture = self._fixture(Path(temporary) / field)
                    fixture.append_capture()
                    fixture.cover_capture_prefix(1)
                    cut = fixture.commit
                    fixture.store.verify_integrity()

                    with fixture.store.direct_recovery_read_scope():
                        horizon = fixture.store.read_root_retrieval_horizon(
                            cut_project_commit=cut
                        )

                    with closing(
                        sqlite3.connect(fixture.paths.database)
                    ) as connection:
                        connection.row_factory = sqlite3.Row
                        row = connection.execute(
                            "SELECT capture_coverage_root_digest, "
                            "capture_coverage_entry_count "
                            "FROM transition_journal WHERE project_commit_no = ?",
                            (cut,),
                        ).fetchone()
                        self.assertIsNotNone(row)
                        assert row is not None
                        if field == "capture_coverage_root_digest":
                            replacement = next(
                                value
                                for value in ("0" * 64, "f" * 64)
                                if value != str(row[field])
                            )
                            connection.execute(
                                "UPDATE transition_journal SET "
                                "capture_coverage_root_digest = ? "
                                "WHERE project_commit_no = ?",
                                (replacement, cut),
                            )
                        else:
                            connection.execute(
                                "UPDATE transition_journal SET "
                                "capture_coverage_entry_count = "
                                "capture_coverage_entry_count + 1 "
                                "WHERE project_commit_no = ?",
                                (cut,),
                            )
                        connection.commit()

                    with self.assertRaises(WorkspaceIntegrityError):
                        WorkspaceStore.open(fixture.paths)
                    with self.assertRaises(WorkspaceIntegrityError):
                        with fixture.store.direct_recovery_read_scope():
                            fixture.store.read_root_capture_page(
                                mission_id="mission.1",
                                cut_project_commit=cut,
                                horizon=horizon,
                                after=None,
                                limit=1,
                            )
                    with self.assertRaises(WorkspaceIntegrityError):
                        fixture.store.verify_integrity()

    def test_schema10_writer_routes_do_not_trust_downgraded_metadata(self):
        with tempfile.TemporaryDirectory() as temporary:
            for operation in ("open", "reissue", "write", "claim"):
                for tamper in ("root_only", "schema_and_root"):
                    with self.subTest(operation=operation, tamper=tamper):
                        fixture = self._fixture(
                            Path(temporary) / f"{operation}-{tamper}"
                        )
                        if operation == "claim":
                            metadata = fixture.store.read_metadata()
                            fixture.store.release_writer(
                                fixture.lease,
                                expected_project_commit=int(
                                    metadata["current_project_commit"]
                                ),
                                expected_root_digest=str(
                                    metadata["current_root_digest"]
                                ),
                                expected_canonical_authority_digest=(
                                    fixture.authority_digest
                                ),
                            )
                        metadata = fixture.store.read_metadata()
                        if tamper == "root_only":
                            self._tamper(
                                fixture,
                                "UPDATE workspace_metadata "
                                "SET root_digest_version = 5",
                            )
                        else:
                            with closing(
                                sqlite3.connect(fixture.paths.database)
                            ) as connection:
                                connection.execute("PRAGMA user_version = 9")
                                connection.execute(
                                    "UPDATE workspace_metadata SET "
                                    "schema_version = 9, root_digest_version = 5"
                                )
                                connection.commit()
                        with self.assertRaises(WorkspaceIntegrityError):
                            if operation == "open":
                                with patch.object(
                                    WorkspaceStore,
                                    "verify_integrity",
                                    side_effect=AssertionError(
                                        "invalid schema10 tuple entered full audit"
                                    ),
                                ):
                                    WorkspaceStore.open(fixture.paths)
                            elif operation == "reissue":
                                fixture.store.reissue_writer_lease(
                                    fixture.principal
                                )
                            elif operation == "write":
                                fixture.append_branch()
                            else:
                                fixture.store.claim_writer(
                                    owner=stable_principal_owner_binding(
                                        fixture.principal
                                    ),
                                    creation_basis=(
                                        "must-not-adopt-downgraded-metadata"
                                    ),
                                    expected_project_commit=int(
                                        metadata["current_project_commit"]
                                    ),
                                    expected_root_digest=str(
                                        metadata["current_root_digest"]
                                    ),
                                    expected_canonical_authority_digest=(
                                        fixture.authority_digest
                                    ),
                                )

    def test_native_root6_rejects_unowned_schema10_migration_provenance(self):
        operations = ("open", "read", "write")
        with tempfile.TemporaryDirectory() as temporary:
            for operation in operations:
                with self.subTest(operation=operation):
                    fixture = self._fixture(Path(temporary) / operation)
                    before = fixture.store.read_metadata()
                    with closing(
                        sqlite3.connect(fixture.paths.database)
                    ) as connection:
                        connection.execute(
                            "INSERT INTO workspace_root_contract_transition("
                            "transition_id, command_id, writer_epoch, "
                            "migration_execution_id, migration_attempt_id, "
                            "plan_sha256, verified_backup_manifest_sha256, "
                            "source_project_commit, source_root_digest, "
                            "source_transition_head_digest, "
                            "canonical_authority_digest, source_schema_version, "
                            "target_schema_version, source_root_digest_version, "
                            "target_root_digest_version, "
                            "target_migration_set_digest, "
                            "target_schema_object_digest, canonical_effect, "
                            "created_at, row_digest"
                            ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 9, 10, "
                            "5, 6, ?, ?, 'none', ?, ?)",
                            (
                                "workspace-root-contract.orphan",
                                "workspace-schema-root-transition.orphan",
                                fixture.lease.epoch,
                                "migration.execution.orphan",
                                "migration.attempt.orphan",
                                "1" * 64,
                                "2" * 64,
                                int(before["current_project_commit"]),
                                str(before["current_root_digest"]),
                                before["transition_head_digest"],
                                fixture.authority_digest,
                                "3" * 64,
                                "4" * 64,
                                _CLOCK(),
                                "5" * 64,
                            ),
                        )
                        connection.commit()

                    with self.assertRaises(WorkspaceIntegrityError):
                        if operation == "open":
                            WorkspaceStore.open(fixture.paths)
                        elif operation == "read":
                            with fixture.store.direct_recovery_read_scope():
                                fixture.store.read_root_retrieval_cut()
                        else:
                            fixture.append_branch()

                    with closing(
                        sqlite3.connect(fixture.paths.database)
                    ) as connection:
                        after_commit = connection.execute(
                            "SELECT current_project_commit FROM workspace_metadata "
                            "WHERE singleton = 1"
                        ).fetchone()
                        self.assertIsNotNone(after_commit)
                        assert after_commit is not None
                        self.assertEqual(
                            int(after_commit[0]),
                            int(before["current_project_commit"]),
                        )
                        command_id = (
                            f"root6.branch."
                            f"{int(before['current_project_commit']) + 1:06d}"
                        )
                        self.assertIsNone(
                            connection.execute(
                                "SELECT 1 FROM command_result "
                                "WHERE command_id = ?",
                                (command_id,),
                            ).fetchone()
                        )

    def test_claim_writer_validates_the_exact_predecessor_epoch_row(self):
        with tempfile.TemporaryDirectory() as temporary:
            fixture = self._fixture(Path(temporary) / "workspace")
            metadata = fixture.store.read_metadata()
            fixture.store.release_writer(
                fixture.lease,
                expected_project_commit=int(metadata["current_project_commit"]),
                expected_root_digest=str(metadata["current_root_digest"]),
                expected_canonical_authority_digest=fixture.authority_digest,
            )
            metadata = fixture.store.read_metadata()
            second_lease = fixture.store.claim_writer(
                owner=stable_principal_owner_binding(fixture.principal),
                creation_basis="unjournaled-second-writer-epoch",
                expected_project_commit=int(metadata["current_project_commit"]),
                expected_root_digest=str(metadata["current_root_digest"]),
                expected_canonical_authority_digest=fixture.authority_digest,
            )
            fixture.store.release_writer(
                second_lease,
                expected_project_commit=int(metadata["current_project_commit"]),
                expected_root_digest=str(metadata["current_root_digest"]),
                expected_canonical_authority_digest=fixture.authority_digest,
            )
            metadata = fixture.store.read_metadata()
            self._tamper(
                fixture,
                "UPDATE writer_epoch SET creation_basis = ? WHERE epoch = 2",
                ("forged-without-row-digest",),
            )
            with self.assertRaisesRegex(
                WorkspaceIntegrityError, "writer epoch tail is corrupt"
            ):
                fixture.store.claim_writer(
                    owner=stable_principal_owner_binding(fixture.principal),
                    creation_basis="exact-predecessor-validation",
                    expected_project_commit=int(metadata["current_project_commit"]),
                    expected_root_digest=str(metadata["current_root_digest"]),
                    expected_canonical_authority_digest=fixture.authority_digest,
                )

    def test_current_dependency_advance_rolls_back_with_the_owner_command(self):
        with tempfile.TemporaryDirectory() as temporary:
            fixture = self._fixture(Path(temporary) / "workspace")

            def snapshot():
                with closing(sqlite3.connect(fixture.paths.database)) as connection:
                    projection = connection.execute(
                        "SELECT * FROM current_dependency_projection"
                    ).fetchall()
                    nodes = connection.execute(
                        "SELECT * FROM current_dependency_node ORDER BY scope_key"
                    ).fetchall()
                    metadata = connection.execute(
                        "SELECT current_project_commit, current_root_digest, "
                        "transition_head_digest FROM workspace_metadata"
                    ).fetchall()
                    return projection, nodes, metadata

            before = snapshot()

            original_advance = store_module._apply_current_dependency_changes

            def fail_after_projection(*args, **kwargs):
                original_advance(*args, **kwargs)
                raise RuntimeError("injected post-projection fault")

            with patch.object(
                store_module,
                "_apply_current_dependency_changes",
                side_effect=fail_after_projection,
            ) as advance:
                with self.assertRaisesRegex(
                    RuntimeError,
                    "post-projection fault",
                ):
                    fixture.append_branch()
            self.assertEqual(advance.call_count, 1)
            self.assertEqual(snapshot(), before)
            with closing(sqlite3.connect(fixture.paths.database)) as connection:
                self.assertIsNone(
                    connection.execute(
                        "SELECT 1 FROM command_result "
                        "WHERE command_id = 'root6.branch.000011'"
                    ).fetchone()
                )

    def test_hook_index_write_rolls_back_and_the_same_request_can_retry(self):
        with tempfile.TemporaryDirectory() as temporary:
            fixture = self._fixture(Path(temporary) / "workspace")

            def snapshot():
                with closing(sqlite3.connect(fixture.paths.database)) as connection:
                    return {
                        "metadata": connection.execute(
                            "SELECT current_project_commit, current_root_digest, "
                            "transition_head_digest FROM workspace_metadata"
                        ).fetchall(),
                        "branch_revision": connection.execute(
                            "SELECT * FROM branch_revision ORDER BY object_id, revision"
                        ).fetchall(),
                        "branch_head": connection.execute(
                            "SELECT * FROM branch_head ORDER BY object_id"
                        ).fetchall(),
                        "hook_nodes": connection.execute(
                            "SELECT * FROM root_hook_index_node ORDER BY node_digest"
                        ).fetchall(),
                        "commits": connection.execute(
                            "SELECT * FROM project_commit ORDER BY commit_no"
                        ).fetchall(),
                        "journals": connection.execute(
                            "SELECT * FROM transition_journal ORDER BY sequence_no"
                        ).fetchall(),
                        "results": connection.execute(
                            "SELECT * FROM command_result ORDER BY command_id"
                        ).fetchall(),
                        "dependency_nodes": connection.execute(
                            "SELECT * FROM current_dependency_node "
                            "ORDER BY scope_key"
                        ).fetchall(),
                        "dependency_projection": connection.execute(
                            "SELECT * FROM current_dependency_projection"
                        ).fetchall(),
                        "coverage_nodes": connection.execute(
                            "SELECT * FROM capture_artifact_coverage_node "
                            "ORDER BY node_digest"
                        ).fetchall(),
                    }

            branch_id = "branch.root6.hook-index-rollback"
            command_id = "root6.branch.hook-index-rollback"

            def write():
                return fixture.store.commit_branch_revision(
                    executive_epoch_id=fixture.epoch_id,
                    mission_id="mission.1",
                    branch_id=branch_id,
                    payload={
                        "branch_id": branch_id,
                        "mission_id": "mission.1",
                        "meaning": "Exercise atomic hook-index persistence.",
                        "recombination_interfaces": (
                            {"interface": "One exact rollback test interface."},
                        ),
                    },
                    expected_head_revision=None,
                    expected_head_payload_digest=None,
                    lease=fixture.lease,
                    command_id=command_id,
                    actor="root6-fixture",
                    expected_canonical_authority_digest=fixture.authority_digest,
                )

            before = snapshot()
            original = store_module._persist_root_hook_indexes

            def fail_after_hook_nodes(connection, **kwargs):
                original(connection, **kwargs)
                self.assertGreater(
                    int(
                        connection.execute(
                            "SELECT COUNT(*) FROM root_hook_index_node"
                        ).fetchone()[0]
                    ),
                    len(before["hook_nodes"]),
                )
                raise RuntimeError("injected post-hook-index fault")

            with patch.object(
                store_module,
                "_persist_root_hook_indexes",
                side_effect=fail_after_hook_nodes,
            ):
                with self.assertRaisesRegex(
                    RuntimeError, "post-hook-index fault"
                ):
                    write()
            self.assertEqual(snapshot(), before)

            outcome = write()
            self.assertEqual(outcome.changed_heads[0].object_id.value, branch_id)
            self.assertGreater(len(snapshot()["hook_nodes"]), len(before["hook_nodes"]))
            with closing(sqlite3.connect(fixture.paths.database)) as connection:
                bound = json.loads(
                    str(
                        connection.execute(
                            "SELECT changed_head_rows_json FROM transition_journal "
                            "WHERE command_id = ?",
                            (command_id,),
                        ).fetchone()[0]
                    )
                )[0]
            self.assertEqual(
                set(bound["root_hook_indexes"]),
                {"hook_item", "strategy_connection"},
            )
            fixture.store.verify_integrity()

    def test_hook_index_full_audit_rejects_missing_altered_extra_and_cross_project_nodes(self):
        with tempfile.TemporaryDirectory() as temporary:
            fixture = self._fixture(Path(temporary) / "workspace")
            fixture.store.verify_integrity()
            with closing(sqlite3.connect(fixture.paths.database)) as connection:
                connection.row_factory = sqlite3.Row
                columns = [
                    str(row["name"])
                    for row in connection.execute(
                        "PRAGMA table_info(root_hook_index_node)"
                    )
                ]
                self.assertEqual(columns[-1], "value_json")
                self.assertTrue(
                    all(
                        columns.index(column) < columns.index("value_json")
                        for column in (
                            "value_digest",
                            "left_node_digest",
                            "left_height",
                            "left_entry_count",
                            "right_node_digest",
                            "right_height",
                            "right_entry_count",
                            "height",
                            "subtree_entry_count",
                        )
                    )
                )
                base = connection.execute(
                    "SELECT * FROM root_hook_index_node ORDER BY node_digest LIMIT 1"
                ).fetchone()
                self.assertIsNotNone(base)
                assert base is not None
                journal_index = store_module._validated_journal_index(
                    connection,
                    project_id="project.rh",
                )

                for name, sql, parameters in (
                    (
                        "missing",
                        "DELETE FROM root_hook_index_node WHERE node_digest = ?",
                        (str(base["node_digest"]),),
                    ),
                    (
                        "altered-value",
                        "UPDATE root_hook_index_node SET value_json = ? "
                        "WHERE node_digest = ?",
                        ('"altered"', str(base["node_digest"])),
                    ),
                    (
                        "cross-project",
                        "UPDATE root_hook_index_node SET project_id = ? "
                        "WHERE node_digest = ?",
                        ("project.poison", str(base["node_digest"])),
                    ),
                ):
                    with self.subTest(corruption=name):
                        connection.execute("SAVEPOINT root_hook_index_tamper")
                        connection.execute(sql, parameters)
                        with self.assertRaises(WorkspaceIntegrityError):
                            store_module._verify_root_hook_indexes_full(
                                connection,
                                project_id="project.rh",
                                root6_start_project_commit=0,
                                journal_index=journal_index,
                            )
                        connection.execute("ROLLBACK TO root_hook_index_tamper")
                        connection.execute("RELEASE root_hook_index_tamper")

                owner_reference = {
                    "kind": str(base["owner_kind"]),
                    "identity": str(base["owner_identity"]),
                    "revision": int(base["owner_revision"]),
                    "payload_sha256": str(base["owner_payload_digest"]),
                }
                _commitment, extra_nodes = store_module._build_root_hook_index(
                    project_id="project.rh",
                    owner_reference=owner_reference,
                    index_family="hook_item",
                    entries=((
                        {
                            "hook_class": "unresolved",
                            "semantic_field": "obligations",
                            "item_index": 999_999,
                        },
                        "cryptographically valid but unreachable extra node",
                    ),),
                )
                extra = next(iter(extra_nodes.values()))
                connection.execute("SAVEPOINT root_hook_index_extra")
                connection.execute(
                    "INSERT INTO root_hook_index_node("
                    + ", ".join(store_module._ROOT_HOOK_NODE_COLUMNS)
                    + ") VALUES ("
                    + ", ".join("?" for _ in store_module._ROOT_HOOK_NODE_COLUMNS)
                    + ")",
                    tuple(
                        getattr(extra, column)
                        for column in store_module._ROOT_HOOK_NODE_COLUMNS
                    ),
                )
                with self.assertRaisesRegex(
                    WorkspaceIntegrityError,
                    "missing, extra, altered, or unreachable",
                ):
                    store_module._verify_root_hook_indexes_full(
                        connection,
                        project_id="project.rh",
                        root6_start_project_commit=0,
                        journal_index=journal_index,
                    )
                connection.execute("ROLLBACK TO root_hook_index_extra")
                connection.execute("RELEASE root_hook_index_extra")

                store_module._verify_root_hook_indexes_full(
                    connection,
                    project_id="project.rh",
                    root6_start_project_commit=0,
                    journal_index=journal_index,
                )

    def test_capture_coverage_full_audit_rejects_projection_tampering(self):
        with tempfile.TemporaryDirectory() as temporary:
            fixture = self._fixture(Path(temporary) / "workspace")
            fixture.append_capture()
            dedicated_noncovering_capture_id = fixture.captures[-1]
            fixture.cover_capture_prefix(1)
            partial_evidence = prepare_evidence_meaning(
                authority=fixture.authority,
                evidence_id="evidence.root6.coverage.noncovering-mission",
                statement="One exact artifact was only partly reviewed.",
                exact_scope="one partial Capture artifact scope",
                strength="diagnostic",
                semantic_role="result",
                authority_basis="direct review of exact captured material",
                sources=(
                    CaptureScope(
                        dedicated_noncovering_capture_id,
                        0,
                        {"coverage": "partial artifact"},
                    ),
                ),
                non_inferences=("does not establish complete coverage",),
            )
            commit_evidence_meaning(
                fixture.store,
                record=partial_evidence,
                lease=fixture.lease,
                actor="root6-fixture",
            )
            inactive_annotation = prepare_capture_scope_annotation(
                authority=fixture.authority,
                annotation_id="annotation.root6.coverage.noncovering-origin",
                capture_id=dedicated_noncovering_capture_id,
                exact_scope={
                    "artifact_ordinal": 0,
                    "coverage": "partial artifact",
                },
                lifecycle="removed",
            )
            commit_capture_scope_annotation(
                fixture.store,
                record=inactive_annotation,
                lease=fixture.lease,
                actor="root6-fixture",
            )
            fixture.append_branch()
            with fixture.store._connection() as connection:
                journal_index = store_module._validated_journal_index(
                    connection,
                    project_id="project.rh",
                )
                store_module._verify_capture_coverage_projection_full(
                    connection,
                    project_id="project.rh",
                    journal_index=journal_index,
                    root6_start_project_commit=0,
                )
                latest = journal_index.journals[-1]
                self.assertIsNotNone(latest.capture_coverage_root_digest)
                self.assertIsNotNone(latest.capture_coverage_entry_count)
                assert latest.capture_coverage_root_digest is not None
                assert latest.capture_coverage_entry_count is not None
                prior = next(
                    item
                    for item in reversed(journal_index.journals[:-1])
                    if item.capture_coverage_root_digest is not None
                    and item.capture_coverage_root_digest
                    != latest.capture_coverage_root_digest
                    and item.capture_coverage_entry_count
                    == latest.capture_coverage_entry_count
                )

                def forged_index(**changes):
                    journals = list(journal_index.journals)
                    journals[-1] = replace(latest, **changes)
                    return replace(journal_index, journals=tuple(journals))

                for name, changed_index in (
                    (
                        "substituted-stale-root",
                        forged_index(
                            capture_coverage_root_digest=(
                                prior.capture_coverage_root_digest
                            )
                        ),
                    ),
                    (
                        "wrong-count",
                        forged_index(
                            capture_coverage_entry_count=(
                                latest.capture_coverage_entry_count + 1
                            )
                        ),
                    ),
                ):
                    with self.subTest(corruption=name):
                        with self.assertRaises(WorkspaceIntegrityError):
                            store_module._verify_capture_coverage_projection_full(
                                connection,
                                project_id="project.rh",
                                journal_index=changed_index,
                                root6_start_project_commit=0,
                            )

                for name, sql, parameters in (
                    (
                        "missing-root-node",
                        "DELETE FROM capture_artifact_coverage_node "
                        "WHERE node_digest = ?",
                        (latest.capture_coverage_root_digest,),
                    ),
                    (
                        "altered-node",
                        "UPDATE capture_artifact_coverage_node "
                        "SET coverer_count = coverer_count + 1 "
                        "WHERE node_digest = ?",
                        (latest.capture_coverage_root_digest,),
                    ),
                ):
                    with self.subTest(corruption=name):
                        connection.execute("SAVEPOINT capture_coverage_tamper")
                        connection.execute(sql, parameters)
                        with self.assertRaises(WorkspaceIntegrityError):
                            store_module._verify_capture_coverage_projection_full(
                                connection,
                                project_id="project.rh",
                                journal_index=journal_index,
                                root6_start_project_commit=0,
                            )
                        connection.execute("ROLLBACK TO capture_coverage_tamper")
                        connection.execute("RELEASE capture_coverage_tamper")

                evidence_source = connection.execute(
                    "SELECT evidence_id, evidence_revision, source_ordinal "
                    "FROM evidence_capture_source WHERE evidence_id = ? "
                    "ORDER BY evidence_revision, source_ordinal LIMIT 1",
                    (fixture.covering_evidence.evidence_id,),
                ).fetchone()
                assert evidence_source is not None
                evidence_id = str(evidence_source["evidence_id"])
                evidence_revision = int(evidence_source["evidence_revision"])
                revision_origin_key = (
                    store_module.AuxiliaryTable.EVIDENCE_ITEM_REVISION.value,
                    store_module.canonical_json_bytes(
                        {
                            "evidence_id": evidence_id,
                            "revision": evidence_revision,
                        }
                    ),
                )
                source_origin_key = (
                    store_module.AuxiliaryTable.EVIDENCE_CAPTURE_SOURCE.value,
                    store_module.canonical_json_bytes(
                        {
                            "evidence_id": evidence_id,
                            "evidence_revision": evidence_revision,
                            "source_ordinal": int(
                                evidence_source["source_ordinal"]
                            ),
                        }
                    ),
                )
                source_only_origins = dict(journal_index.auxiliary_origins)
                source_only_original = source_only_origins[source_origin_key]
                self.assertEqual(len(source_only_original), 1)
                source_only_origins[source_origin_key] = (
                    (latest, source_only_original[0][1]),
                )
                with self.assertRaisesRegex(
                    WorkspaceIntegrityError,
                    "outside its exact origin",
                ):
                    store_module._verify_capture_coverage_projection_full(
                        connection,
                        project_id="project.rh",
                        journal_index=replace(
                            journal_index,
                            auxiliary_origins=source_only_origins,
                        ),
                        root6_start_project_commit=0,
                    )

                future_origins = dict(journal_index.auxiliary_origins)
                for origin_key in (revision_origin_key, source_origin_key):
                    original_origins = future_origins[origin_key]
                    self.assertEqual(len(original_origins), 1)
                    future_origins[origin_key] = (
                        (latest, original_origins[0][1]),
                    )
                with self.assertRaisesRegex(
                    WorkspaceIntegrityError,
                    "selected future Evidence data",
                ):
                    store_module._verify_capture_coverage_projection_full(
                        connection,
                        project_id="project.rh",
                        journal_index=replace(
                            journal_index,
                            auxiliary_origins=future_origins,
                        ),
                        root6_start_project_commit=0,
                    )

                capture_origin_key = (
                    store_module.AuxiliaryTable.RAW_CAPTURE.value,
                    store_module.canonical_json_bytes(
                        {"capture_id": dedicated_noncovering_capture_id}
                    ),
                )
                future_capture_origins = dict(journal_index.auxiliary_origins)
                capture_origins = future_capture_origins[capture_origin_key]
                self.assertEqual(len(capture_origins), 1)
                future_capture_origins[capture_origin_key] = (
                    (latest, capture_origins[0][1]),
                )
                with self.assertRaisesRegex(
                    WorkspaceIntegrityError,
                    "Evidence source precedes its exact Capture",
                ):
                    store_module._verify_capture_coverage_projection_full(
                        connection,
                        project_id="project.rh",
                        journal_index=replace(
                            journal_index,
                            auxiliary_origins=future_capture_origins,
                        ),
                        root6_start_project_commit=0,
                    )

                annotation_origin_key = (
                    store_module.AuxiliaryTable.CAPTURE_SCOPE_ANNOTATION_REVISION.value,
                    store_module.canonical_json_bytes(
                        {
                            "annotation_id": inactive_annotation.annotation_id,
                            "revision": inactive_annotation.revision,
                        }
                    ),
                )
                annotation_origins = dict(journal_index.auxiliary_origins)
                annotation_original = annotation_origins[annotation_origin_key]
                self.assertEqual(len(annotation_original), 1)
                capture_origin_commit = int(
                    capture_origins[0][0].row["project_commit_no"]
                )
                pre_capture = next(
                    item
                    for item in journal_index.journals
                    if int(item.row["project_commit_no"])
                    < capture_origin_commit
                )
                annotation_origins[annotation_origin_key] = (
                    (pre_capture, annotation_original[0][1]),
                )
                with self.assertRaisesRegex(
                    WorkspaceIntegrityError,
                    "annotation precedes or crosses its exact Capture",
                ):
                    store_module._verify_capture_coverage_projection_full(
                        connection,
                        project_id="project.rh",
                        journal_index=replace(
                            journal_index,
                            auxiliary_origins=annotation_origins,
                        ),
                        root6_start_project_commit=0,
                    )

                connection.execute("SAVEPOINT capture_coverage_mission")
                partial_source = connection.execute(
                    "SELECT evidence_id, evidence_revision FROM "
                    "evidence_capture_source WHERE evidence_id = ? "
                    "ORDER BY evidence_revision, source_ordinal LIMIT 1",
                    (partial_evidence.evidence_id,),
                ).fetchone()
                assert partial_source is not None
                subject_row = connection.execute(
                    "SELECT subject_json FROM evidence_item_revision "
                    "WHERE evidence_id = ? AND revision = ?",
                    (
                        str(partial_source["evidence_id"]),
                        int(partial_source["evidence_revision"]),
                    ),
                ).fetchone()
                assert subject_row is not None
                subject = json.loads(str(subject_row["subject_json"]))
                subject["mission_id"] = "mission.poison"
                connection.execute(
                    "UPDATE evidence_item_revision SET subject_json = ? "
                    "WHERE evidence_id = ? AND revision = ?",
                    (
                        store_module.canonical_json_bytes(subject).decode("utf-8"),
                        str(partial_source["evidence_id"]),
                        int(partial_source["evidence_revision"]),
                    ),
                )
                with self.assertRaisesRegex(
                    WorkspaceIntegrityError,
                    "crosses its exact Mission",
                ):
                    store_module._verify_capture_coverage_projection_full(
                        connection,
                        project_id="project.rh",
                        journal_index=journal_index,
                        root6_start_project_commit=0,
                    )
                connection.execute("ROLLBACK TO capture_coverage_mission")
                connection.execute("RELEASE capture_coverage_mission")

                capture_id = fixture.captures[0]
                for name, mission_id, coverer_count in (
                    ("duplicate-extra-logical-key", "mission.1", 17),
                    ("cross-mission-poison", "mission.poison", 1),
                ):
                    with self.subTest(corruption=name):
                        connection.execute("SAVEPOINT capture_coverage_extra")
                        extra_tree = store_module._CaptureCoverageMutation(
                            connection,
                            project_id="project.rh",
                            commitment=store_module._CaptureCoverageCommitment(
                                store_module._capture_coverage_empty_root(
                                    "project.rh"
                                ),
                                0,
                            ),
                        )
                        extra_tree.set_count(
                            mission_id=mission_id,
                            capture_id=capture_id,
                            artifact_ordinal=0,
                            coverer_count=coverer_count,
                            require_absent=True,
                        )
                        poisoned = extra_tree.finish(persist=True)
                        selected_index = (
                            journal_index
                            if mission_id == "mission.1"
                            else forged_index(
                                capture_coverage_root_digest=poisoned.root_digest,
                                capture_coverage_entry_count=(
                                    poisoned.entry_count
                                ),
                            )
                        )
                        with self.assertRaises(WorkspaceIntegrityError):
                            store_module._verify_capture_coverage_projection_full(
                                connection,
                                project_id="project.rh",
                                journal_index=selected_index,
                                root6_start_project_commit=0,
                            )
                        connection.execute("ROLLBACK TO capture_coverage_extra")
                        connection.execute("RELEASE capture_coverage_extra")

                store_module._verify_capture_coverage_projection_full(
                    connection,
                    project_id="project.rh",
                    journal_index=journal_index,
                    root6_start_project_commit=0,
                )

    def test_capture_coverage_writer_rolls_back_and_replays_at_every_seam(self):
        seam_names = (
            "after-semantic-delta",
            "after-in-memory-apply",
            "after-node-persist",
            "after-root-compute",
            "after-final-boundary",
        )
        with tempfile.TemporaryDirectory() as temporary:
            for seam in seam_names:
                with self.subTest(seam=seam):
                    fixture = self._fixture(Path(temporary) / seam)
                    fixture.append_capture()
                    capture_id = fixture.captures[-1]
                    record = prepare_evidence_meaning(
                        authority=fixture.authority,
                        evidence_id=f"evidence.root6.rollback.{seam}",
                        statement="This exact Capture artifact was reviewed.",
                        exact_scope="one exact Capture artifact",
                        strength="exact_finite_identity",
                        semantic_role="result",
                        authority_basis="direct review of exact captured material",
                        sources=(
                            CaptureScope(
                                capture_id,
                                0,
                                {"coverage": "complete artifact"},
                            ),
                        ),
                        non_inferences=(
                            "does not establish a mathematical theorem",
                        ),
                    )
                    command_id = f"root6.coverage.rollback.{seam}"

                    def snapshot():
                        with closing(
                            sqlite3.connect(fixture.paths.database)
                        ) as snapshot_connection:
                            tables = tuple(
                                str(row[0])
                                for row in snapshot_connection.execute(
                                    "SELECT name FROM sqlite_master "
                                    "WHERE type = 'table' "
                                    "AND name NOT LIKE 'sqlite_%' ORDER BY name"
                                )
                            )
                            return tuple(
                                (
                                    table,
                                    tuple(
                                        snapshot_connection.execute(
                                            f'SELECT * FROM "{table}" ORDER BY rowid'
                                        )
                                    ),
                                )
                                for table in tables
                            )

                    before = snapshot()
                    with ExitStack() as stack:
                        if seam == "after-semantic-delta":
                            original = (
                                store_module._capture_coverage_transaction_changes
                            )

                            def fail_after_semantic_delta(*args, **kwargs):
                                original(*args, **kwargs)
                                raise RuntimeError(seam)

                            stack.enter_context(
                                patch.object(
                                    store_module,
                                    "_capture_coverage_transaction_changes",
                                    new=fail_after_semantic_delta,
                                )
                            )
                        elif seam == "after-in-memory-apply":
                            original = store_module._apply_capture_coverage_changes

                            def fail_after_in_memory_apply(*args, **kwargs):
                                original(*args, **kwargs)
                                raise RuntimeError(seam)

                            stack.enter_context(
                                patch.object(
                                    store_module,
                                    "_apply_capture_coverage_changes",
                                    new=fail_after_in_memory_apply,
                                )
                            )
                        elif seam == "after-node-persist":
                            original = store_module._CaptureCoverageMutation.finish

                            def fail_after_node_persist(mutation, *args, **kwargs):
                                result = original(mutation, *args, **kwargs)
                                if kwargs.get("persist") is True:
                                    raise RuntimeError(seam)
                                return result

                            stack.enter_context(
                                patch.object(
                                    store_module._CaptureCoverageMutation,
                                    "finish",
                                    new=fail_after_node_persist,
                                )
                            )
                        elif seam == "after-root-compute":
                            original = WorkspaceStore._head_root_digest

                            def fail_after_root_compute(store, *args, **kwargs):
                                original(store, *args, **kwargs)
                                raise RuntimeError(seam)

                            stack.enter_context(
                                patch.object(
                                    WorkspaceStore,
                                    "_head_root_digest",
                                    new=fail_after_root_compute,
                                )
                            )
                        else:
                            original = WorkspaceStore._verify_current_boundary

                            def fail_after_final_boundary(store, *args, **kwargs):
                                original(store, *args, **kwargs)
                                raise RuntimeError(seam)

                            stack.enter_context(
                                patch.object(
                                    WorkspaceStore,
                                    "_verify_current_boundary",
                                    new=fail_after_final_boundary,
                                )
                            )
                        with self.assertRaisesRegex(RuntimeError, seam):
                            commit_evidence_meaning(
                                fixture.store,
                                record=record,
                                lease=fixture.lease,
                                actor="root6-fixture",
                                command_id=command_id,
                            )

                    self.assertEqual(snapshot(), before)
                    committed = commit_evidence_meaning(
                        fixture.store,
                        record=record,
                        lease=fixture.lease,
                        actor="root6-fixture",
                        command_id=command_id,
                    )
                    self.assertFalse(committed.replayed)
                    after_success = snapshot()
                    replayed = commit_evidence_meaning(
                        fixture.store,
                        record=record,
                        lease=fixture.lease,
                        actor="root6-fixture",
                        command_id=command_id,
                    )
                    self.assertTrue(replayed.replayed)
                    self.assertEqual(replayed.project_commit, committed.project_commit)
                    self.assertEqual(snapshot(), after_success)
                    with fixture.store.direct_recovery_read_scope():
                        cut = fixture.store.read_root_retrieval_cut()
                        connection = (
                            fixture.store._direct_recovery_read_connection.get()
                        )
                        assert connection is not None
                        commitment = (
                            store_module._capture_coverage_commitment_at_cut(
                                connection,
                                project_id="project.rh",
                                project_commit_no=int(cut["project_commit"]),
                            )
                        )
                        tree = store_module._CaptureCoverageMutation(
                            connection,
                            project_id="project.rh",
                            commitment=commitment,
                        )
                        self.assertEqual(
                            tree.lookup(
                                mission_id="mission.1",
                                capture_id=capture_id,
                                artifact_ordinal=0,
                            ),
                            1,
                        )
                    fixture.store.verify_integrity()

    def test_epoch_terminal_transition_updates_authenticated_current_continuity(self):
        with tempfile.TemporaryDirectory() as temporary:
            fixture = self._fixture(Path(temporary) / "workspace")
            chain = fixture.store.read_active_executive_epoch("mission.1")
            self.assertIsNotNone(chain)
            assert chain is not None
            failed = prepare_direct_executive_epoch_failed_before_checkpoint_event(
                executive_epoch_id=fixture.epoch_id,
                mission_id="mission.1",
                reconciliation={
                    "stage": "goal_runtime",
                    "failure_reason": "synthetic terminal transition",
                },
            )
            outcome = fixture.store.fail_executive_epoch_before_checkpoint(
                mission_id="mission.1",
                executive_epoch_id=fixture.epoch_id,
                event=failed,
                expected_event_ordinal=int(chain[-1]["event_ordinal"]),
                expected_event_digest=str(chain[-1]["event_digest"]),
                lease=fixture.lease,
                command_id="root6.epoch.fail",
                actor="root6-fixture",
                expected_canonical_authority_digest=fixture.authority_digest,
            )
            self.assertEqual(outcome.project_commit, 11)
            self.assertIsNone(
                fixture.store.read_active_executive_epoch("mission.1")
            )
            latest = fixture.store.read_latest_executive_epoch_events(
                "mission.1"
            )
            self.assertEqual(latest[-1]["event_kind"], "failed_before_checkpoint")
            with fixture.store._connection(read_only=True) as connection:
                state = store_module._current_mission_continuity(
                    connection,
                    project_id="project.rh",
                    mission_id="mission.1",
                )
            self.assertIsNotNone(state)
            assert state is not None
            self.assertIsNone(state["active_executive_epoch_id"])
            self.assertEqual(
                state["latest_epoch"]["event_kind"],
                "failed_before_checkpoint",
            )
            fixture.store.verify_integrity()

    def test_unaccessed_history_waits_for_full_audit_or_exact_access(self):
        changes = {
            "old_revision": "UPDATE branch_revision SET payload_json = ? WHERE object_id = 'branch.root6.000006'",
            "old_journal": "UPDATE transition_journal SET actor = ? WHERE project_commit_no = 4",
            "old_result": "UPDATE command_result SET result_json = ? WHERE project_commit_no = 4",
            "old_head": "UPDATE branch_head SET payload_digest = ? WHERE object_id = 'branch.root6.000006'",
            "old_transition_predecessor": "UPDATE transition_journal SET predecessor_digest = ? WHERE project_commit_no = 4",
            "old_auxiliary": "UPDATE raw_capture SET provenance_json = ? WHERE observation_id = 'observation.root6.000005'",
            "old_foreign_key": "UPDATE raw_capture_artifact SET blob_sha256 = ? WHERE capture_id = (SELECT capture_id FROM raw_capture WHERE observation_id = 'observation.root6.000005')",
        }
        with tempfile.TemporaryDirectory() as temporary:
            for name, sql in changes.items():
                with self.subTest(corruption=name):
                    fixture = self._fixture(Path(temporary) / name)
                    self._tamper(fixture, sql, ("0" * 64,))
                    reopened = WorkspaceStore.open(fixture.paths)
                    with self.assertRaises(WorkspaceIntegrityError):
                        reopened.verify_integrity()
                    if name == "old_revision":
                        with self.assertRaises(WorkspaceIntegrityError):
                            reopened.get_revision(fixture.branches[0])
                    if name == "old_head":
                        with self.assertRaises(WorkspaceIntegrityError):
                            reopened.get_head(TypedWorkspaceId(IdentityKind.BRANCH, "branch.root6.000006"))


if __name__ == "__main__":
    unittest.main()
