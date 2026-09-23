"""Candidate-selected Context reliance is not generic retained-reference traversal.

The reader fake supplies exact hashed owner documents at the existing read
boundary. These tests exercise the real A1/Admission basis constructor, not
Store authentication, migration, or a model's choice of scientific premises.
"""

from __future__ import annotations

import hashlib
import sys
import unittest
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Mapping


PACKAGE_ROOT = Path(__file__).resolve().parents[1]
REPO_ROOT = Path(__file__).resolve().parents[3]
if str(PACKAGE_ROOT) not in sys.path:
    sys.path.insert(0, str(PACKAGE_ROOT))

from research_core.candidate_revision import (  # noqa: E402
    _prepare_candidate_revision_v2_legacy_document,
    candidate_schema_digest,
    candidate_schema_digest_v3,
    prepare_candidate_revision_v3,
    prepare_candidate_revision_v4,
    validate_candidate_revision_v4,
)
from research_core.context_revision import context_reference_projection  # noqa: E402
from research_core.complete_claim_admission import (  # noqa: E402
    ADMISSION_REVIEWER_ROLE,
    AdmissionEvidenceRef,
    AdmissionOwnerRevisionRef,
)
from research_core.json_support import canonical_json_bytes  # noqa: E402
from research_core.mission_executive import (  # noqa: E402
    OwnerRevisionRef,
    ResolvedOwnerRevision,
)
from research_core.mission_interface import MissionInterface  # noqa: E402
from research_core.research_model import deep_thaw  # noqa: E402
from test_candidate_successor import _authority  # noqa: E402


_OMITTED = object()


def _candidate_ref(reference: OwnerRevisionRef, selection: Any = _OMITTED) -> dict:
    result = {
        "kind": reference.kind,
        "id": reference.identity,
        "revision": reference.revision,
        "digest_sha256": reference.payload_sha256,
    }
    if selection is not _OMITTED:
        result["selection"] = selection
    return result


def _selection(*treatment_ids: str) -> dict:
    return {"mode": "treatments", "treatment_ids": list(treatment_ids)}


class _ExactOwnerReader:
    def __init__(self, root_version: int) -> None:
        self.root_version = root_version
        self.rows: dict[OwnerRevisionRef, ResolvedOwnerRevision] = {}
        self.reads: list[OwnerRevisionRef] = []
        self.scope_depth = 0
        self.bound_calls: list[dict] = []

    def add(self, kind: str, identity: str, document: Mapping, revision=1):
        reference = OwnerRevisionRef(
            kind, identity, revision,
            hashlib.sha256(canonical_json_bytes(document)).hexdigest(),
        )
        outgoing_document = document
        if kind == "context" and self.root_version != 6 and document.get("schema_version") in (1, 2):
            # Preserve the actual legacy direct-checkpoint resolver. The A1
            # root6 reader historically uses full exact-reference extraction.
            outgoing_document = document["owner_source_references"]
        self.rows[reference] = ResolvedOwnerRevision(
            reference=reference,
            mission_id="mission.1",
            validated_document=document,
            outgoing_refs=MissionInterface._direct_owner_refs(
                list(MissionInterface._exact_references_in(outgoing_document))
            ),
            is_current_head=not (identity == "evidence.E" and revision == 1),
        )
        return reference

    def resolve(self, reference):
        self.reads.append(reference)
        return self.rows[reference]

    def read_metadata(self):
        return {"root_digest_version": self.root_version}

    @contextmanager
    def direct_recovery_read_scope(self):
        self.scope_depth += 1
        try:
            yield
        finally:
            self.scope_depth -= 1

    def _read_current_bound_owner_revision(self, **kwargs):
        if self.scope_depth != 1:
            raise AssertionError("root6 basis reads must retain the authenticated scope")
        self.bound_calls.append(kwargs)
        row = self.resolve(OwnerRevisionRef(**kwargs))
        return {
            "reference": row.reference.to_mapping(),
            "mission_id": row.mission_id,
            "document": row.validated_document,
            "selected_revision_state": (
                "current_head" if row.is_current_head else "historical_revision"
            ),
        }


class _BasisFixture:
    def __init__(self, root_version=6, *, historical_reliance=False):
        self.reader = _ExactOwnerReader(root_version)
        self.refs = {}
        for label, identity, revision in (
            ("E1", "evidence.E", 1), ("E2", "evidence.E", 2),
            ("R", "evidence.R", 1), ("H", "evidence.H", 1),
            ("U", "evidence.U", 1),
        ):
            self.refs[label] = self.reader.add(
                "evidence", identity,
                {"kind": "evidence", "statement": label, "mission_id": "mission.1"},
                revision,
            )

        def source(label, roles, dependency=None):
            return {
                "reference": self.refs[label].to_mapping(), "selection": None,
                "roles": roles, "why": f"Authored use of {label}.",
                "dependency": dependency,
            }

        self.context_document = {
            "schema_version": 3, "kind": "first_class_context",
            "project_id": "project.rh", "mission_id": "mission.1",
            "context_id": "context.scientific", "purpose": "Keep the parent program.",
            "question": "Which construction reaches the parent question?",
            "treatments": {
                "selected": {
                    "question": "Can the exact contour be reused?",
                    "account": "Use the historical theorem at its stated normalization.",
                    "qualifications": ["Only the stated contour and normalization."],
                    "sources": [
                        source("E1", ["reliance"], {
                            "observed_current_reference": self.refs["E2"].to_mapping(),
                            "change_that_matters": "A normalization correction.",
                            "dependent_judgment": "The contour can be reused.",
                        }),
                        source("R", ["recognition"]),
                        source("H", ["history", "reliance"] if historical_reliance else ["history"]),
                    ],
                },
                "unselected": {
                    "question": "Does the alternative kernel work?",
                    "account": "An independent treatment of the other kernel.",
                    "qualifications": [], "sources": [source("U", ["reliance"])],
                },
            },
            "exposed_treatments": [], "known_omissions": [],
            "restricted_uses": [], "restrictions": [],
            "independence_treatment": {"basis": "No independent review asserted."},
        }
        self.context = self.reader.add(
            "context", "context.scientific", self.context_document,
        )
        self.interface = object.__new__(MissionInterface)
        self.interface._mission_id = "mission.1"
        self.interface._store = self.reader
        self.interface._resolve_direct_checkpoint_owner = self.reader.resolve
        self.interface._candidate_a1_binding_for_ref = lambda **kwargs: None

    def candidate(self, *, selection=_OMITTED, extra_refs=(), argument_refs=(), version=4):
        constructor = {
            2: _prepare_candidate_revision_v2_legacy_document,
            3: prepare_candidate_revision_v3,
            4: prepare_candidate_revision_v4,
        }[version]
        kwargs = {
            "authority": _authority(), "candidate_id": "candidate.context-basis",
            "proposal_kind": "lemma", "exact_statement": "The contour transfer holds.",
            "standing": {"status": "open", "basis": "Exact reconstruction is required."},
            "supporting_refs": [_candidate_ref(self.context, selection), *extra_refs],
            "objections": ["The normalization may defeat the claimed transfer."],
            "repo_root": REPO_ROOT,
        }
        if argument_refs:
            kwargs["argument_edges"] = [{
                "edge_id": "edge.transfer", "edge_kind": "composition",
                "premises": ["The selected exact construction."],
                "conclusion": "The parent transfer follows at the stated scope.",
                "authority": {"class": "evidence_interpretation", "refs": argument_refs},
            }]
        record = constructor(**kwargs)
        reference = self.reader.add("candidate", "candidate.context-basis", record.document)
        return {"reference": reference.to_mapping(), "document": record.document}

    def basis(self, candidate):
        rows = self.interface._candidate_a1_review_source_closure(candidate)
        return {canonical_json_bytes(row) for row in rows}

    def expected(self, *labels):
        return {
            canonical_json_bytes(reference.to_mapping())
            for reference in (self.context, *(self.refs[label] for label in labels))
        }


class CandidateContextBasisTests(unittest.TestCase):
    def test_selected_basis_does_not_promote_recognition_history_or_current_head(self):
        for root_version in (5, 6):
            with self.subTest(root_version=root_version):
                fixture = _BasisFixture(root_version)
                candidate = fixture.candidate(selection=_selection("selected"))
                original = canonical_json_bytes(candidate["document"])
                self.assertEqual(fixture.basis(candidate), fixture.expected("E1"))
                for label in ("R", "H", "E2", "U"):
                    self.assertNotIn(fixture.refs[label], fixture.reader.reads)
                self.assertEqual(canonical_json_bytes(candidate["document"]), original)
                self.assertEqual(tuple(candidate["document"]["objections"]), (
                    "The normalization may defeat the claimed transfer.",
                ))
                self.assertEqual(fixture.reader.scope_depth, 0)
                self.assertEqual(bool(fixture.reader.bound_calls), root_version == 6)

    def test_genuine_historical_reliance_and_independent_candidate_reference_survive(self):
        for root_version in (5, 6):
            for historical_reliance in (False, True):
                with self.subTest(root_version=root_version, history=historical_reliance):
                    fixture = _BasisFixture(root_version, historical_reliance=historical_reliance)
                    candidate = fixture.candidate(
                        selection=_selection("selected"),
                        extra_refs=[_candidate_ref(fixture.refs["R"])],
                    )
                    labels = ("E1", "R", "H") if historical_reliance else ("E1", "R")
                    self.assertEqual(fixture.basis(candidate), fixture.expected(*labels))

    def test_same_context_selected_again_through_argument_edge_unions_reliance(self):
        for root_version in (5, 6):
            with self.subTest(root_version=root_version):
                fixture = _BasisFixture(root_version)
                candidate = fixture.candidate(
                    selection=_selection("selected"),
                    argument_refs=[_candidate_ref(fixture.context, _selection("unselected"))],
                )
                self.assertEqual(fixture.basis(candidate), fixture.expected("E1", "U"))

    def test_transitive_context_source_keeps_its_exact_treatment_selection(self):
        for root_version in (5, 6):
            with self.subTest(root_version=root_version):
                fixture = _BasisFixture(root_version)
                deeper_context = fixture.context
                outer = deep_thaw(fixture.context_document)
                outer["context_id"] = "context.outer"
                outer["treatments"]["selected"]["sources"] = [{
                    "reference": deeper_context.to_mapping(),
                    "selection": _selection("selected"),
                    "roles": ["reliance"],
                    "why": "Use only the exact contour treatment.",
                    "dependency": None,
                }]
                fixture.context = fixture.reader.add("context", "context.outer", outer)
                basis = fixture.basis(fixture.candidate(selection=_selection("selected")))
                self.assertEqual(
                    basis,
                    fixture.expected("E1") | {canonical_json_bytes(deeper_context.to_mapping())},
                )
                for label in ("R", "H", "E2", "U"):
                    self.assertNotIn(fixture.refs[label], fixture.reader.reads)

    def test_missing_selected_treatment_does_not_fabricate_whole_context_reliance(self):
        fixture = _BasisFixture()
        candidate = fixture.candidate(selection=_selection("absent"))
        # Candidate preservation validates authored shape, not proof completeness.
        self.assertTrue(validate_candidate_revision_v4(candidate["document"]).ok)
        with self.assertRaisesRegex(ValueError, "selected scientific treatment is unavailable"):
            fixture.basis(candidate)

    def test_authored_whole_context_and_retained_unscoped_candidates_are_not_narrowed(self):
        for root_version in (5, 6):
            for version, selection in ((2, _OMITTED), (3, _OMITTED), (4, _OMITTED),
                                       (4, {"mode": "whole_context"})):
                with self.subTest(root_version=root_version, version=version, selection=selection):
                    fixture = _BasisFixture(root_version)
                    candidate = fixture.candidate(version=version, selection=selection)
                    self.assertEqual(fixture.basis(candidate), fixture.expected("E1", "U"))

    def test_legacy_context_keeps_the_preexisting_exact_basis_projection(self):
        for root_version in (5, 6):
            for context_version in (1, 2):
                with self.subTest(root_version=root_version, context_version=context_version):
                    fixture = _BasisFixture(root_version)
                    document = deep_thaw(fixture.context_document)
                    document["schema_version"] = context_version
                    del document["treatments"]
                    del document["exposed_treatments"]
                    document["indispensable_ground"] = [{
                        "reference": fixture.refs["E1"].to_mapping(),
                        "why": "Exact historical theorem retained as ground.",
                    }]
                    document["owner_source_references"] = [{
                        "reference": fixture.refs[label].to_mapping(),
                        "retrieval": f"evidence:{fixture.refs[label].identity}@1",
                        "provenance": "Explicitly authored legacy Context source.",
                    } for label in ("E1", "R")]
                    document["invalidation_conditions"] = []
                    if context_version == 2:
                        document["immutable_historical_references"] = [{
                            "reference": fixture.refs["H"].to_mapping(),
                            "why": "Retained source in the legacy frozen closure.",
                        }]
                        document["untrusted_material_locators"] = []
                        document["historical_advisory_scope"] = {
                            "assignment_mode": "lifecycle_historian",
                            "search_lens": "Recover exact contour lineage.",
                            "source_families": ["evidence"],
                            "known_omissions": [], "coverage_limits": [],
                        }
                    fixture.context = fixture.reader.add(
                        "context", "context.scientific", document,
                    )
                    labels = ("E1", "R", "H") if context_version == 2 and root_version == 6 else ("E1", "R")
                    self.assertEqual(fixture.basis(fixture.candidate()), fixture.expected(*labels))

    def test_projection_consumers_have_distinct_exact_reference_sets(self):
        fixture = _BasisFixture()
        expected = {
            "retention": {"E1", "E2", "R", "H", "U"},
            "discovery": {"E1", "R", "H", "U"},
            "concurrency": {"E2"},
            "mathematical_basis": {"E1"},
        }
        for purpose, labels in expected.items():
            with self.subTest(purpose=purpose):
                kwargs = {"treatment_ids": ["selected"]} if purpose == "mathematical_basis" else {}
                projection = context_reference_projection(
                    fixture.context_document, purpose=purpose, **kwargs,
                )
                self.assertEqual(
                    {canonical_json_bytes(row["reference"]) for row in projection},
                    {canonical_json_bytes(fixture.refs[label].to_mapping()) for label in labels},
                )

    def test_v4_selection_is_closed_and_cannot_be_attached_to_non_context_sources(self):
        fixture = _BasisFixture()
        valid = fixture.candidate(selection=_selection("selected"))
        self.assertTrue(validate_candidate_revision_v4(valid["document"]).ok)
        self.assertEqual(valid["document"]["schema_version"], 4)
        for selection in (
            {"mode": "treatments", "treatment_ids": []},
            {"mode": "current"},
            {"mode": "whole_context", "treatment_ids": ["selected"]},
        ):
            invalid = deep_thaw(valid["document"])
            invalid["supporting_refs"][0]["selection"] = selection
            with self.subTest(selection=selection):
                self.assertFalse(validate_candidate_revision_v4(invalid).ok)
        for selection in (None, _selection("selected")):
            invalid = deep_thaw(valid["document"])
            invalid["supporting_refs"] = [_candidate_ref(fixture.refs["R"], selection)]
            result = validate_candidate_revision_v4(invalid)
            with self.subTest(non_context_selection=selection):
                self.assertEqual(result.ok, selection is None)

    def test_context_selection_participates_in_candidate_digest_without_editing_history(self):
        fixture = _BasisFixture()
        selected = fixture.candidate(selection=_selection("selected"))
        unselected = fixture.candidate(selection=_selection("unselected"))
        self.assertNotEqual(selected["reference"]["payload_sha256"],
                            unselected["reference"]["payload_sha256"])
        self.assertEqual(candidate_schema_digest(REPO_ROOT),
                         "e56333a0de2ae1ab6bbb6e1127fdb462c4ee4d04654a6b36bc110bc18a44ff49")
        self.assertEqual(candidate_schema_digest_v3(REPO_ROOT),
                         "59e6a89797320d54f37c6261bafd23a4015ce8b3d46b64ca15f5947a807fe0fa")

    def test_a1_and_admission_present_full_context_as_container_not_all_premises(self):
        for retrieval_kind in ("a1", "admission"):
            with self.subTest(retrieval_kind=retrieval_kind):
                fixture = _BasisFixture()
                candidate = fixture.candidate(
                    selection=_selection("selected"),
                    extra_refs=[_candidate_ref(fixture.refs["R"])],
                )
                source_closure = fixture.interface._candidate_a1_review_source_closure(candidate)
                assignment_document = deep_thaw(fixture.context_document)
                assignment_document["context_id"] = "context.review"
                assignment_document["purpose"] = "Review the frozen Candidate without promoting it."
                assignment_document["treatments"] = {}
                assignment_ref = fixture.reader.add("context", "context.review", assignment_document)

                def read_owner(*, kind, identity, revision):
                    matches = [
                        row for reference, row in fixture.reader.rows.items()
                        if (reference.kind, reference.identity, reference.revision)
                        == (kind, identity, revision)
                    ]
                    self.assertEqual(len(matches), 1)
                    row = fixture.reader.resolve(matches[0].reference)
                    return {"reference": row.reference.to_mapping(), "document": row.validated_document}

                fixture.interface._read_recovery_owner_revision = read_owner
                fixture.interface._candidate_a1_review_candidate = lambda _reference: candidate
                candidate_ref = {
                    "candidate_id": candidate["reference"]["identity"],
                    "revision": candidate["reference"]["revision"],
                    "payload_sha256": candidate["reference"]["payload_sha256"],
                }
                grant = {
                    "grant_id": "grant.context-basis-review",
                    "candidate_ref": candidate_ref,
                    "context": {
                        "id": "context:context.review", "revision": assignment_ref.revision,
                        "payload_sha256": assignment_ref.payload_sha256,
                    },
                    "source_closure": source_closure,
                }
                if retrieval_kind == "a1":
                    result = fixture.interface._candidate_a1_review_retrieve({}, grant)
                else:
                    # The Case reader is an owner boundary, not the behavior
                    # under test: present the already-frozen closure unchanged.
                    case_document = {
                        "kind": "complete_claim_admission_case",
                        "candidate_ref": candidate_ref,
                        "source_closure": deep_thaw(source_closure),
                    }
                    case_ref = AdmissionEvidenceRef(
                        "evidence.admission-case", 1,
                        hashlib.sha256(canonical_json_bytes(case_document)).hexdigest(),
                    )
                    case = SimpleNamespace(
                        candidate_ref=SimpleNamespace(
                            candidate_id=candidate_ref["candidate_id"],
                            revision=candidate_ref["revision"],
                            digest_sha256=candidate_ref["payload_sha256"],
                        ),
                        source_closure=tuple(
                            AdmissionOwnerRevisionRef.from_payload(reference)
                            for reference in source_closure
                        ),
                        evidence=SimpleNamespace(to_payload=lambda: case_document),
                    )

                    def read_case(reference):
                        self.assertEqual(reference, case_ref)
                        return case

                    fixture.reader.read_complete_claim_admission_case = read_case
                    grant.update({
                        "case_ref": case_ref.to_payload(), "review_ref": None,
                        "role": ADMISSION_REVIEWER_ROLE,
                    })
                    result = fixture.interface._admission_retrieve({}, grant)

                source_items = [
                    row for row in result["items"]
                    if row["role"] == "frozen_context_source_container"
                ]
                self.assertEqual(len(source_items), 1)
                item = source_items[0]
                self.assertEqual(item["reference"], fixture.context.to_mapping())
                self.assertEqual(canonical_json_bytes(item["document"]),
                                 canonical_json_bytes(fixture.context_document))
                self.assertEqual(set(item["document"]["treatments"]), {"selected", "unselected"})
                self.assertIn("only reliance-role sources", item["mathematical_use"])
                self.assertIn("objections retain their full scope", item["mathematical_use"])
                evidence_items = [
                    row for row in result["items"]
                    if row["role"] == "candidate_authored_mathematical_basis"
                ]
                self.assertEqual(
                    {canonical_json_bytes(row["reference"]) for row in evidence_items},
                    {canonical_json_bytes(fixture.refs[label].to_mapping()) for label in ("E1", "R")},
                )
                for row in evidence_items:
                    self.assertNotIn("mathematical_use", row)
                frozen_candidate = next(
                    row for row in result["items"] if row["role"] == "frozen_candidate_a1"
                )
                self.assertEqual(canonical_json_bytes(frozen_candidate["document"]),
                                 canonical_json_bytes(candidate["document"]))
                self.assertEqual(tuple(frozen_candidate["document"]["objections"]), (
                    "The normalization may defeat the claimed transfer.",
                ))
                self.assertIsNone(result["next_cursor"])
                self.assertEqual(result["canonical_effect"], "none")
                self.assertEqual(result["public_effect"], "none")


if __name__ == "__main__":
    unittest.main()
