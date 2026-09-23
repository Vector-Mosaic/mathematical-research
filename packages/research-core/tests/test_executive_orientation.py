from __future__ import annotations

import hashlib
import json
import sys
import unittest
from collections.abc import Mapping
from pathlib import Path
from unittest import mock

PACKAGE_ROOT = Path(__file__).resolve().parents[1]
TEST_ROOT = Path(__file__).resolve().parent
for location in (PACKAGE_ROOT, TEST_ROOT):
    if str(location) not in sys.path:
        sys.path.insert(0, str(location))

import test_mission_interface_direct as direct_fixture
from research_core.executive_orientation import (
    _canonical_status,
    failed_output_is_relevant,
    recovery_facts_in_snapshot,
    scientific_context_in_snapshot,
    semantic_owner_document,
    source_qualification_document,
)
from research_core.context_revision import issue_context_revision_v3, read_context_revision
from research_core.json_support import canonical_json_bytes
from research_core.mission_frontier import (
    read_mission_strategy_head,
    prepare_strategy_revision,
    commit_strategy_revision,
)
from research_core.research_model import deep_thaw
from research_core.workspace_store import WorkspaceIntegrityError


class _ExcludedBody(Mapping):
    """An excluded subtree must never be inspected, even before discarding it."""

    def __getitem__(self, key):
        raise AssertionError("excluded body was read")

    def __iter__(self):
        raise AssertionError("excluded body was traversed")

    def __len__(self):
        raise AssertionError("excluded body was measured")


class ExecutiveOrientationTests(unittest.TestCase):
    """O01-O11/O14-O15 and owner-side proof/Formal/Host companion evidence."""

    @classmethod
    def setUpClass(cls):
        direct_fixture.DirectMissionInterfaceTests.setUpClass.__func__(cls)

    setUp = direct_fixture.DirectMissionInterfaceTests.setUp
    _request = staticmethod(direct_fixture.DirectMissionInterfaceTests._request)
    _authorize = direct_fixture.DirectMissionInterfaceTests._authorize
    _bind = direct_fixture.DirectMissionInterfaceTests._bind
    _execute = direct_fixture.DirectMissionInterfaceTests._execute
    _capture_pending_material = (
        direct_fixture.DirectMissionInterfaceTests._capture_pending_material
    )
    _linux_principal = staticmethod(
        direct_fixture.DirectMissionInterfaceTests._linux_principal
    )

    def _epoch(self):
        epoch = self._authorize()
        self._bind(epoch)
        return epoch

    def _strategy(self, epoch, **changes):
        current = read_mission_strategy_head(
            self.store, mission_id=self.interface.mission_id
        )
        parameters = {
            key: deep_thaw(value)
            for key, value in current.record.document.items()
            if key not in {"schema_version", "kind"}
        }
        parameters.update(changes)
        prepared = prepare_strategy_revision(self.store, **parameters)
        commit_strategy_revision(
            self.store,
            authority=self.interface._direct_epoch_authority(epoch),
            prepared=prepared,
            lease=self.interface._writer_lease(),
            actor="orientation-test",
            expected_dependency_heads={},
        )
        return read_mission_strategy_head(
            self.store, mission_id=self.interface.mission_id
        )

    def _stored_state(self):
        return (
            dict(self.store.read_metadata()),
            self.store.paths.database.read_bytes(),
            self.canonical.authorized_path.read_bytes(),
        )

    @staticmethod
    def _scientific_treatment(account, *, sources=(), qualifications=()):
        return {
            "question": "Which qualified synthetic result survives for the parent program?",
            "account": account,
            "qualifications": list(qualifications),
            "sources": list(sources),
        }

    @staticmethod
    def _scientific_source(reference, *, roles=("reliance",), selection=None):
        return {
            "reference": deep_thaw(reference),
            "selection": selection,
            "roles": list(roles),
            "why": "Use only the exact selected synthetic mathematics.",
            "dependency": None,
        }

    def _scientific_context(self, epoch, identity, treatments, *, exposed=(), previous=None, metadata=None):
        """Publish real fixture owners through the scientific mutation boundary."""
        metadata = {} if metadata is None else metadata
        if previous is None:
            record = issue_context_revision_v3(
                authority=self.interface._direct_epoch_authority(epoch),
                context_id=identity,
                purpose="Synthetic Mission-wide understanding, not Strategy selection.",
                question="What remains established across the parent program?",
                treatments=treatments,
                exposed_treatments=list(exposed),
                known_omissions=metadata.get("known_omissions", []),
                restricted_uses=metadata.get("restricted_uses", []),
                restrictions=metadata.get("restrictions", ["Synthetic structural fixture; no mathematical proof."]),
                independence_treatment=metadata.get("independence_treatment", {"approach": "Synthetic fixture; no independence claim."}),
            )
            expected = None
            create = deep_thaw(record.document)
            patch = None
        else:
            expected = {
                "kind": "context",
                "identity": identity,
                "revision": previous.revision,
                "payload_sha256": previous.payload_digest,
            }
            old = previous.record.document["treatments"]
            create = None
            patch = {
                "insert": {key: value for key, value in treatments.items() if key not in old},
                "replace": {key: value for key, value in treatments.items() if key in old},
                "remove": [
                    {"treatment_id": key, "reason": "Explicit fixture withdrawal."}
                    for key in old if key not in treatments
                ],
                "exposure_add": [],
                "exposure_remove": [],
                "metadata_replace": metadata,
            }
        result = self._execute(
            "record_context",
            {
                "mode": "scientific",
                "updates": [{
                    "context_id": f"context:{identity}",
                    "expected_head": expected,
                    "create": create,
                    "patch": patch,
                }],
                "exposure_required": [],
            },
            executive_epoch_id=epoch,
        )
        self.assertEqual(result["status"], "completed", result)
        return read_context_revision(self.store, context_id=identity)

    def _scientific_projection(self, context_id):
        # This isolates projection, not owner binding authorization. Every Context,
        # exact source and head comes from the real Store in the same held cut.
        with self.store.direct_recovery_read_scope():
            return self._scientific_projection_in_snapshot(context_id)

    def _scientific_projection_in_snapshot(self, context_id):
        reference, _ = self.interface._resolve_owner_selector({"id": "mission:mission.1"})
        mission = deep_thaw(self.interface._read_recovery_owner_revision(
            kind="mission", identity="mission.1", revision=reference["revision"],
        ))
        mission["document"]["scientific_context_id"] = context_id
        return scientific_context_in_snapshot(self.interface, mission)

    def test_scientific_context_unbound_is_explicit_closed_and_read_only(self):
        before = self._stored_state()
        orientation = self.interface.executive_orientation()
        self.assertEqual(orientation["schema_version"], "mathematical_research.executive_orientation.v3")
        self.assertEqual(orientation["scientific_context"], {
            "state": "unbound",
            "binding": None,
            "root_reference": None,
            "purpose": None,
            "question": None,
            "known_omissions": [],
            "restricted_uses": [],
            "restrictions": [],
            "independence_treatment": None,
            "treatments": [],
            "source_changes": [],
            "unavailable": [],
        })
        self.assertEqual(self._stored_state(), before)

    def test_scientific_exposure_is_root_local_plus_explicit_nonrecursive_selection(self):
        epoch = self._epoch()
        leaf = self._scientific_context(epoch, "context.science.leaf", {
            "leaf": self._scientific_treatment("UNSELECTED-GRANDCHILD-BODY"),
        })
        child = self._scientific_context(epoch, "context.science.child", {
            "chosen": self._scientific_treatment("Chosen interface survives."),
            "other": self._scientific_treatment("UNSELECTED-SIBLING-BODY"),
        }, exposed=[{"context_id": leaf.record.document["context_id"], "treatment_id": "leaf"}],
            metadata={"restrictions": ["fixed-order only"]})
        root_metadata = {
            "known_omissions": ["The unselected branch remains scientifically unresolved."],
            "restricted_uses": ["Do not infer a uniform limit from these finite constructions."],
            "restrictions": ["All root treatments retain the positive-gap hypothesis."],
            "independence_treatment": {"approach": "Synthetic account; no independent proof review."},
        }
        root = self._scientific_context(epoch, "context.science.root", {
            "parent": self._scientific_treatment("Keep the parent program alive."),
            "alternative": self._scientific_treatment("Compare the wider construction."),
        }, exposed=[{"context_id": child.record.document["context_id"], "treatment_id": "chosen"}], metadata=root_metadata)
        before = self._stored_state()
        science = self._scientific_projection("context.science.root")
        self.assertEqual(science["state"], "available")
        self.assertEqual(science["root_reference"]["revision"], root.revision)
        for field, value in root_metadata.items():
            self.assertEqual(science[field], value)
        # Independent scientific exposure does not require a Strategy citation.
        self.assertNotIn("strategy_ground", self.interface.executive_orientation())
        self.assertEqual({
            (item["context_reference"]["identity"], item["treatment_id"])
            for item in science["treatments"]
        }, {
            ("context.science.root", "parent"),
            ("context.science.root", "alternative"),
            ("context.science.child", "chosen"),
        })
        self.assertEqual(science["source_changes"], [])
        for item in science["treatments"]:
            if item["context_reference"]["identity"] == "context.science.root":
                self.assertNotIn("context_qualifications", item)
            else:
                self.assertEqual(item["context_qualifications"], {
                    field: deep_thaw(child.record.document[field])
                    for field in ("known_omissions", "restricted_uses", "restrictions", "independence_treatment")
                })
                self.assertEqual(item["context_qualifications"]["restrictions"], ["fixed-order only"])
        self.assertNotIn("UNSELECTED-GRANDCHILD-BODY", json.dumps(science))
        self.assertNotIn("UNSELECTED-SIBLING-BODY", json.dumps(science))
        self.assertEqual(self._stored_state(), before)

        read_head = self.store.get_head
        local_failure = "context revision identity, payload, or full-row digest mismatch"

        def fail_child(object_id):
            if object_id.key == "context:context.science.child":
                raise WorkspaceIntegrityError(local_failure)
            return read_head(object_id)

        with mock.patch.object(self.store, "get_head", side_effect=fail_child):
            partial = self._scientific_projection("context.science.root")
        self.assertEqual(partial["state"], "partial")
        for field, value in root_metadata.items():
            self.assertEqual(partial[field], value)
        self.assertEqual({item["treatment_id"] for item in partial["treatments"]}, {"parent", "alternative"})
        self.assertEqual(partial["unavailable"][0]["context_id"], "context.science.child")
        self.assertEqual(partial["unavailable"][0]["reason_code"], "integrity_failure")
        self.assertNotIn(local_failure, json.dumps(partial))
        local_failure = "historical journal commitment failed"
        with mock.patch.object(self.store, "get_head", side_effect=fail_child):
            # A valid current metadata root cannot downgrade arbitrary historical
            # commitment failure to one missing scientific treatment.
            with self.assertRaisesRegex(WorkspaceIntegrityError, "historical journal"):
                self._scientific_projection("context.science.root")

    def test_scientific_context_source_correction_keeps_exact_treatment_scope(self):
        epoch = self._epoch()
        used = self._scientific_treatment(
            "A finite endpoint construction survives.",
            qualifications=["Normalization is 2*pi; no uniform endpoint conclusion."],
        )
        original = self._scientific_context(epoch, "context.science.source", {
            "used": used,
            "unrelated": self._scientific_treatment("Original unrelated account."),
        })
        pinned, _ = self.interface._resolve_owner_selector({"id": "context:context.science.source"})
        selection = {"mode": "treatments", "treatment_ids": ["used"]}
        parent = self._scientific_treatment("Use one qualified interface, not its enclosing archive.", sources=[
            self._scientific_source(pinned, selection=selection),
        ])
        self._scientific_context(epoch, "context.science.root", {"parent": parent})
        self.assertEqual(self._scientific_projection("context.science.root")["source_changes"], [])

        restricted = self._scientific_context(
            epoch, "context.science.source", deep_thaw(original.record.document["treatments"]),
            previous=original, metadata={"restrictions": ["All treatments require n>=N"]},
        )
        self.assertEqual(restricted.record.document["treatments"], original.record.document["treatments"])
        restriction_change = self._scientific_projection("context.science.root")["source_changes"][0]
        self.assertEqual(restriction_change["cited_reference"], pinned)
        self.assertEqual(restriction_change["qualification"]["treatments"], {"used": used})
        self.assertEqual(restriction_change["qualification"]["context_qualifications"]["restrictions"], [
            "All treatments require n>=N",
        ])

        grown = self._scientific_context(epoch, "context.science.source", {
            "used": used,
            "unrelated": self._scientific_treatment("UNRELATED-GROWTH-" + "x" * 65536),
            "new-unrelated": self._scientific_treatment("NEW-UNRELATED-TREATMENT"),
        }, previous=restricted)
        science = self._scientific_projection("context.science.root")
        self.assertEqual(len(science["source_changes"]), 1)
        change = science["source_changes"][0]
        self.assertEqual(change["cited_reference"], pinned)
        self.assertEqual(change["selection"], selection)
        self.assertEqual(change["current_reference"]["revision"], grown.revision)
        self.assertEqual(change["state"], "advanced_same_identity")
        self.assertEqual(change["qualification"], {
            "selection": selection,
            "treatments": {"used": used},
            "context_qualifications": {
                field: deep_thaw(grown.record.document[field])
                for field in ("known_omissions", "restricted_uses", "restrictions", "independence_treatment")
            },
        })
        self.assertIsNone(change["qualification_ref"])
        self.assertNotIn("UNRELATED-GROWTH", json.dumps(science))
        self.assertNotIn("NEW-UNRELATED-TREATMENT", json.dumps(science))

        corrected = {**used, "qualifications": [
            "Corrected normalization is pi, not 2*pi.",
            "Scope: compact positive domain; hypothesis: positive gap.",
            "For each fixed degree only; no uniform limit or RH conclusion.",
        ]}
        current = self._scientific_context(epoch, "context.science.source", {
            **deep_thaw(grown.record.document["treatments"]), "used": corrected,
        }, previous=grown)
        science = self._scientific_projection("context.science.root")
        change = science["source_changes"][0]
        self.assertEqual(change["cited_reference"], pinned)
        self.assertEqual(change["current_reference"]["revision"], current.revision)
        self.assertEqual(change["qualification"]["treatments"], {"used": corrected})
        self.assertEqual(science["treatments"][0]["content"]["sources"][0]["reference"], pinned)
        self.assertEqual(change["affected"][0]["roles"], ["reliance"])

        self._scientific_context(epoch, "context.science.source", {
            key: deep_thaw(value) for key, value in current.record.document["treatments"].items()
            if key != "used"
        }, previous=current)
        missing = self._scientific_projection("context.science.root")
        self.assertEqual(missing["state"], "partial")
        self.assertTrue(any(
            item["kind"] == "context" and item["context_id"] == "context.science.source"
            and item["treatment_id"] == "used" and item["reason_code"] == "not_found"
            for item in missing["unavailable"]
        ))
        self.assertNotIn("UNRELATED-GROWTH", json.dumps(missing))

    def test_scientific_source_changes_preserve_qualifications_not_history_only_bodies(self):
        epoch = self._epoch()
        capture_id = self._capture_pending_material(epoch)
        first = self._evidence(epoch, capture_id, normalization="old normalization")
        history = self._evidence(epoch, capture_id, identity="evidence.history-only")
        pinned, _ = self.interface._resolve_owner_selector({"id": "evidence:evidence.startup"})
        historical, _ = self.interface._resolve_owner_selector({"id": "evidence:evidence.history-only"})
        self._scientific_context(epoch, "context.science.root", {
            "qualified": self._scientific_treatment("Retain exact qualified backing.", sources=[
                self._scientific_source(pinned, roles=("recognition", "reliance")),
                self._scientific_source(historical, roles=("history",)),
            ]),
        })
        qualifications = {
            "normalization": "Corrected normalization: pi.",
            "hypotheses": ["Strictly positive synthetic gap."],
            "domain": "Compact positive domain only.",
            "quantifiers": "For each fixed integer degree.",
            "exact_scope": "Finite scope; no unbounded-degree extension.",
            "limitations": ["No uniform endpoint estimate."],
        }
        self._evidence(epoch, capture_id,
            expected_head_revision=first.revision,
            expected_head_payload_digest=first.payload_digest,
            **qualifications,
        )
        self._evidence(epoch, capture_id, identity="evidence.history-only",
            expected_head_revision=history.revision,
            expected_head_payload_digest=history.payload_digest,
            statement="HISTORY-ONLY-CURRENT-BODY-MUST-NOT-APPEAR",
        )
        science = self._scientific_projection("context.science.root")
        self.assertEqual(len(science["source_changes"]), 1)
        change = science["source_changes"][0]
        self.assertEqual(change["cited_reference"], pinned)
        self.assertEqual(change["current_reference"]["revision"], 2)
        qualification = change["qualification"]
        for key in ("normalization", "hypotheses", "domain", "quantifiers"):
            self.assertEqual(qualification["subject"][key], qualifications[key])
        for key in ("exact_scope", "limitations"):
            self.assertEqual(qualification[key], qualifications[key])
        self.assertEqual(change["affected"][0]["roles"], ["recognition", "reliance"])
        self.assertNotIn("HISTORY-ONLY-CURRENT-BODY-MUST-NOT-APPEAR", json.dumps(science))

        read_source = self.store._read_current_bound_owner_revision

        def deny_exact_source(**reference):
            if reference["identity"] == pinned["identity"] and reference["revision"] == 1:
                raise PermissionError("private failure detail must not escape")
            return read_source(**reference)

        with mock.patch.object(self.store, "_read_current_bound_owner_revision", side_effect=deny_exact_source):
            partial = self._scientific_projection("context.science.root")
        self.assertEqual(partial["state"], "partial")
        self.assertEqual(partial["treatments"], science["treatments"])
        self.assertEqual(partial["unavailable"][0]["reference"], pinned)
        self.assertEqual(partial["unavailable"][0]["reason_code"], "access_denied")
        self.assertNotIn("private failure detail", json.dumps(partial))

    def test_scientific_treatment_is_inline_and_current_without_strategy_expansion(self):
        epoch = self._epoch()
        treatment = self._scientific_treatment("One exact qualified body.")
        original = self._scientific_context(epoch, "context.science.root", {"interface/~": treatment})
        pinned, _ = self.interface._resolve_owner_selector({"id": "context:context.science.root"})
        self._strategy(epoch, owner_refs=[pinned])
        orientation = self.interface.executive_orientation()
        self.assertNotIn("strategy_ground", orientation)
        self.assertIn(
            {"id": "context:context.science.root", "revision": 1},
            orientation["current_strategy"]["owner_refs"],
        )
        science = self._scientific_projection("context.science.root")
        entry = science["treatments"][0]
        self.assertEqual(entry["content"], treatment)
        self.assertEqual(entry["context_reference"], pinned)
        self.assertNotIn("content_ref", entry)

        replacement = {**treatment, "qualifications": ["A materially different qualification."]}
        self._scientific_context(epoch, "context.science.root", {"interface/~": replacement}, previous=original)
        science = self._scientific_projection("context.science.root")
        entry = science["treatments"][0]
        self.assertEqual(entry["content"], replacement)
        self.assertEqual(entry["context_reference"]["revision"], 2)
        # Exposure advancing does not rewrite Strategy's historical exact source.
        self.assertIn(
            {"id": "context:context.science.root", "revision": 1},
            self.interface.executive_orientation()["current_strategy"]["owner_refs"],
        )

    def test_scientific_authored_whole_context_use_preserves_its_full_cost(self):
        epoch = self._epoch()
        first = self._scientific_context(epoch, "context.science.source", {
            "first": self._scientific_treatment("Whole-account component one."),
            "second": self._scientific_treatment("Whole-account component two."),
        })
        pinned, _ = self.interface._resolve_owner_selector({"id": "context:context.science.source"})
        selection = {"mode": "whole_context"}
        self._scientific_context(epoch, "context.science.root", {
            "whole": self._scientific_treatment("Explicitly use the whole authored account.", sources=[
                self._scientific_source(pinned, selection=selection),
            ]),
        })
        current = self._scientific_context(epoch, "context.science.source", {
            **deep_thaw(first.record.document["treatments"]),
            "third": self._scientific_treatment("AUTHORED-WHOLE-CONTEXT-GROWTH-" + "x" * 8192),
        }, previous=first)
        change = self._scientific_projection("context.science.root")["source_changes"][0]
        self.assertEqual(change["cited_reference"], pinned)
        self.assertEqual(change["qualification"], {
            "selection": selection,
            "context": semantic_owner_document("context", current.record.document),
        })
        self.assertGreater(len(json.dumps(change["qualification"])), 8192)

    def test_scientific_correction_dedup_keeps_distinct_and_partial_treatment_selections(self):
        epoch = self._epoch()
        first = self._scientific_context(epoch, "context.science.source", {
            key: self._scientific_treatment(f"Synthetic component {key}.")
            for key in ("a", "b", "unselected")
        })
        pinned, _ = self.interface._resolve_owner_selector({"id": "context:context.science.source"})
        self._scientific_context(epoch, "context.science.root", {
            "parent": self._scientific_treatment("Two authored uses of the same revision.", sources=[
                self._scientific_source(pinned, selection={"mode": "treatments", "treatment_ids": ids})
                for ids in (["a"], ["a", "b"])
            ]),
        })
        survivor = self._scientific_treatment("Corrected a.", qualifications=["Fixed-degree scope only."])
        self._scientific_context(epoch, "context.science.source", {
            "a": survivor,
            "unselected": self._scientific_treatment("UNSELECTED-CORRECTION-BODY"),
        }, previous=first)
        science = self._scientific_projection("context.science.root")
        self.assertEqual(science["state"], "partial")
        changes = science["source_changes"]
        self.assertEqual(len(changes), 2)
        self.assertEqual({tuple(change["selection"]["treatment_ids"]) for change in changes}, {
            ("a",), ("a", "b"),
        })
        for change in changes:
            self.assertEqual(change["cited_reference"], pinned)
            self.assertEqual(change["qualification"]["treatments"], {"a": survivor})
        self.assertEqual([item["treatment_id"] for item in science["unavailable"]], ["b"])
        self.assertNotIn("UNSELECTED-CORRECTION-BODY", json.dumps(science))

    def test_scientific_selected_root_absence_is_local_but_shared_integrity_is_fatal(self):
        missing = self._scientific_projection("context.science.absent")
        self.assertEqual(missing["state"], "unavailable")
        self.assertEqual(missing["binding"], {"context_id": "context.science.absent"})
        self.assertIsNone(missing["root_reference"])
        for field in ("known_omissions", "restricted_uses", "restrictions"):
            self.assertEqual(missing[field], [])
        self.assertIsNone(missing["independence_treatment"])
        self.assertEqual(missing["treatments"], [])
        self.assertEqual(missing["unavailable"][0]["reason_code"], "not_found")
        with mock.patch.object(self.store, "read_metadata", side_effect=WorkspaceIntegrityError("shared authority failure")):
            with self.assertRaises(WorkspaceIntegrityError):
                self.interface.executive_orientation()

    def _evidence(self, epoch, capture_id, *, identity="evidence.startup", **changes):
        from research_core.mission_evidence import (
            CaptureScope,
            commit_evidence_meaning,
            prepare_evidence_meaning,
            read_evidence_meaning,
        )

        parameters = {
            "statement": "  Synthetic θ statement\nwith exact whitespace.  ",
            "semantic_role": "conditional fixture implication",
            "authority_basis": "Fixture capture only; not established mathematics.",
            "exact_scope": "The exact finite synthetic fixture, no further case.",
            "strength": "conditional",
            "limitations": ["Endpoint remains unresolved.", "No uniform extension."],
            "non_inferences": ["No complete RH result follows."],
        }
        parameters.update(changes)
        record = prepare_evidence_meaning(
            authority=self.interface._direct_epoch_authority(epoch),
            evidence_id=identity,
            sources=[CaptureScope(capture_id, 0, "Complete synthetic capture.")],
            **parameters,
        )
        commit_evidence_meaning(
            self.store,
            record=record,
            lease=self.interface._writer_lease(),
            actor="evidence-startup-fixture",
        )
        return read_evidence_meaning(self.store, evidence_id=identity)


    def test_evidence_source_qualification_filters_before_copy_and_preserves_qualifiers(self):
        excluded = (
            "authority_basis",
            "dependencies",
            "interpreted_owner_inputs",
            "future_subject_field",
        )
        base = {
            "subtype": "evidence_meaning",
            "subject": {
                "statement": '  θ {"closure":"literal"}\n  ',
                "semantic_role": "Exact fixture role.",
                "objects": ["exact object"],
                "hypotheses": ["positive domain"],
                "domain": "finite interval",
                "normalization": "original normalization",
                "quantifiers": "each fixed parameter",
                "parent_bet_outcome": "unresolved",
                "standing": "conditional",
            },
            "exact_scope": "Only this scoped fixture.",
            "rigor": "conditional",
            "limitations": ["Unresolved case remains."],
            "non_inferences": ["No wider conclusion."],
        }
        for present, value in (
            (False, None),
            (True, ""),
            (True, "  Exact consequence.\n"),
        ):
            expected = deep_thaw(base)
            if present:
                expected["subject"]["decision_consequence"] = value
            source = {
                **expected,
                "subject": {
                    **expected["subject"],
                    **{key: _ExcludedBody() for key in excluded},
                },
                "private_envelope": _ExcludedBody(),
            }
            with self.subTest(decision_consequence_present=present, value=value):
                self.assertEqual(source_qualification_document("evidence", source), expected)

    def test_candidate_argument_bodies_are_excluded_before_copying(self):
        expected = {
            "proposal_kind": "lemma",
            "exact_statement": "Synthetic conditional lemma.",
            "standing": {"status": "open", "basis": "Unverified."},
            "limitations": ["No uniform extension."],
            "non_inferences": ["No complete result."],
        }
        self.assertEqual(
            source_qualification_document(
                "candidate",
                {
                    **expected,
                    "argument_edges": _ExcludedBody(),
                    "argument_steps": _ExcludedBody(),
                    "future_field": _ExcludedBody(),
                },
            ),
            expected,
        )

    def test_huge_evidence_dependencies_stay_lossless_current_and_historical_read(self):
        epoch = self._epoch()
        capture_id = self._capture_pending_material(epoch)
        dependencies = [
            {
                "fixture_index": index,
                "exact_body": "δ😀" + "x" * 4096,
                "nested": {"closure": ["literal", index]},
            }
            for index in range(2048)
        ]
        self.assertGreater(len(json.dumps(dependencies)), 8_000_000)
        prior = None
        first_read = None
        huge_read = None
        for revision, selected in (
            (1, dependencies[:1]),
            (2, dependencies),
            (3, dependencies[:2]),
        ):
            record = self._evidence(
                epoch,
                capture_id,
                dependencies=selected,
                decision_consequence="  Retain the exact conditional consequence.\n",
                objects=["synthetic object"],
                hypotheses=["synthetic hypothesis"],
                domain="finite fixture",
                normalization="fixture normalization",
                quantifiers="for each fixture member",
                parent_bet_outcome="unresolved",
                standing="unverified",
                expected_head_revision=None if prior is None else prior.revision,
                expected_head_payload_digest=None
                if prior is None
                else prior.payload_digest,
            )
            reference, _ = self.interface._resolve_owner_selector(
                {"id": "evidence:evidence.startup"}
            )
            self._strategy(epoch, owner_refs=[reference])
            before_read = self._stored_state()
            orientation = self.interface.executive_orientation()
            self.assertNotIn("strategy_ground", orientation)
            self.assertIn(
                {"id": "evidence:evidence.startup", "revision": revision},
                orientation["current_strategy"]["owner_refs"],
            )
            self.assertNotIn("fixture_index", json.dumps(deep_thaw(orientation)))
            document = record.evidence.to_payload()
            exact = self._execute(
                "retrieve",
                {
                    "mode": "read",
                    "purpose": "Read complete synthetic Evidence deliberately.",
                    "ids": ["evidence:evidence.startup"],
                },
                executive_epoch_id=epoch,
            )
            self.assertEqual(exact["status"], "completed")
            text = exact["result"]["items"][0]["readable_content"]
            readable = json.loads(text)
            self.assertTrue(readable["subject"]["dependencies"] == selected)
            self.assertEqual(readable.pop("source_navigation"), {
                "schema_version": "mathematical_research.evidence_source_navigation.v1",
                "relation": "recorded_capture_scopes",
                "items": [{
                    "ordinal": 0,
                    "handle": f"capture-artifact:{capture_id}#0",
                    "exact_scope": {"description": "Complete synthetic capture."},
                }],
            })
            self.assertTrue(readable == semantic_owner_document("evidence", document))
            self.assertEqual(self._stored_state(), before_read)
            if first_read is None:
                first_read = text
            if revision == 2:
                huge_read = text
            prior = record
        before_read = self._stored_state()
        historical = self._execute(
            "retrieve",
            {
                "mode": "read",
                "purpose": "Read the retained exact prior revision.",
                "ids": ["evidence:evidence.startup@1", "evidence:evidence.startup@2"],
            },
            executive_epoch_id=epoch,
        )
        self.assertEqual(historical["status"], "completed")
        items = historical["result"]["items"]
        self.assertTrue(items[0]["readable_content"] == first_read)
        self.assertTrue(items[1]["readable_content"] == huge_read)
        self.assertTrue(
            json.loads(items[1]["readable_content"])["subject"]["dependencies"]
            == dependencies
        )
        self.assertEqual(self._stored_state(), before_read)

    def test_strategy_references_remain_complete_without_automatic_owner_bodies(
        self,
    ):
        epoch = self._epoch()
        capture_id = self._capture_pending_material(epoch)
        context = self._execute(
            "record_context",
            {
                "context_id": "context:context.five-card-fixture",
                "subject": "Synthetic Context for the five-card contract fixture.",
                "question": "Which exact finite hypotheses apply?",
                "material": [{"id": "mission:mission.1", "why": "Mission scope only."}],
            },
            executive_epoch_id=epoch,
        )
        self.assertEqual(context["status"], "completed")
        context_ref, _ = self.interface._resolve_owner_selector(
            {"id": "context:context.five-card-fixture"}
        )
        records = [
            self._evidence(
                epoch,
                capture_id,
                identity=f"evidence.five-card.{index}",
                statement=f"Synthetic finite fixture statement {index}; no production mathematics.",
                decision_consequence=None
                if index % 2 == 0
                else f"Preserve fixture consequence {index}.",
                dependencies=[{"fixture_only_synthesis": "not startup " * 1000}],
            )
            for index in range(4)
        ]
        references = [
            self.interface._resolve_owner_selector(
                {"id": f"evidence:evidence.five-card.{index}"}
            )[0]
            for index in range(4)
        ] + [context_ref]
        self._strategy(
            epoch,
            **{
                field: []
                for field in (
                    "causal_inputs",
                    "serious_opportunities",
                    "selected_bets",
                    "attention_actions",
                    "context_treatment",
                    "creativity_treatment",
                    "reconsideration_conditions",
                    "reversal_conditions",
                    "revival_conditions",
                )
            },
            owner_refs=references,
        )
        before = self._stored_state()
        with mock.patch.object(
            self.store, "read_raw_capture_artifact_bytes",
            side_effect=AssertionError("Startup loaded a raw artifact body"),
        ):
            snapshot = self.interface.host_snapshot()
        orientation = snapshot["executive_orientation"]
        self.assertNotIn("source_navigation", json.dumps(deep_thaw(orientation)))
        self.assertNotIn("strategy_ground", orientation)
        self.assertCountEqual(
            orientation["current_strategy"]["owner_refs"],
            [{"id": f"{ref['kind']}:{ref['identity']}", "revision": ref["revision"]}
             for ref in references],
        )
        for record in records:
            self.assertNotIn(record.evidence.to_payload()["subject"]["statement"],
                             json.dumps(deep_thaw(orientation)))
        self.assertEqual(orientation["proof_attention"]["open_candidate_a1"], [])
        self.assertEqual(orientation["formal_attention"], [])
        self.assertEqual(
            snapshot["current_state"]["schema_version"],
            "mathematical_research.mission_host_current_state.v2",
        )
        self.assertNotIn("reconstruction", snapshot)
        self.assertNotIn("host_recovery_facts", snapshot)
        self.assertEqual(self._stored_state(), before)

    def test_reserved_evidence_source_qualifications_are_explicit_without_extra_fields(
        self,
    ):
        subjects = {
            "candidate_a1_triage": {
                "candidate_ref": {
                    "candidate_id": "candidate.synthetic",
                    "revision": 1,
                    "digest_sha256": "a" * 64,
                },
                "disposition": "admission_ready",
                "review_finding": "Scoped review finding.",
                "cited_basis": [],
                "concrete_defects": [],
                "no_remaining_material_objection": True,
                "limitations": ["Only this claim."],
                "non_inferences": ["No canonical effect."],
            },
            "complete_claim_admission_case": {
                "candidate_ref": {
                    "candidate_id": "candidate.synthetic",
                    "revision": 1,
                    "digest_sha256": "a" * 64,
                },
                "triage_ref": {
                    "evidence_id": "evidence.triage",
                    "revision": 1,
                    "payload_sha256": "b" * 64,
                },
                "candidate_disposition": "proof",
                "exact_claim": "Synthetic exact claim.",
                "source_closure": [],
            },
            "complete_claim_admission_review": {
                "candidate_ref": {
                    "candidate_id": "candidate.synthetic",
                    "revision": 1,
                    "digest_sha256": "a" * 64,
                },
                "case_ref": {
                    "evidence_id": "evidence.case",
                    "revision": 1,
                    "payload_sha256": "b" * 64,
                },
                "disposition": "material_objection",
                "review_finding": "One unresolved case.",
                "objections": [{"exact_objection": "Synthetic boundary."}],
                "cited_basis": [],
            },
            "complete_claim_admission_decision": {
                "candidate_ref": {
                    "candidate_id": "candidate.synthetic",
                    "revision": 1,
                    "digest_sha256": "a" * 64,
                },
                "case_ref": {
                    "evidence_id": "evidence.case",
                    "revision": 1,
                    "payload_sha256": "b" * 64,
                },
                "review_ref": {
                    "evidence_id": "evidence.review",
                    "revision": 1,
                    "payload_sha256": "c" * 64,
                },
                "disposition": "reject",
                "decision_basis": "The synthetic case is incomplete.",
                "objections": [{"exact_objection": "Synthetic boundary."}],
                "cited_basis": [],
            },
        }
        common = {
            "exact_scope": "Frozen synthetic claim only.",
            "rigor": "unverified_claim",
            "limitations": ["Not independently admitted."],
            "non_inferences": ["No canonical or complete result."],
        }
        for subtype, subject in subjects.items():
            with self.subTest(subtype=subtype):
                clean = {"subtype": subtype, "subject": subject, **common}
                projected = source_qualification_document(
                    "evidence",
                    {
                        **clean,
                        "subject": {
                            **subject,
                            "future_proof_body": _ExcludedBody(),
                            "grant_machinery": _ExcludedBody(),
                        },
                    },
                )
                self.assertEqual(projected, semantic_owner_document("evidence", clean))
                self.assertEqual(set(projected["subject"]), set(subject))
        subject = {
            "kind": "purported_complete_route",
            "candidate_id": "candidate.synthetic",
            "claimed_scope": ["claim.rh.complete"],
        }
        projected = source_qualification_document(
            "evidence",
            {
                "subtype": "purported_complete_route",
                **common,
                "subject": {
                    **subject,
                    **{
                        key: _ExcludedBody()
                        for key in (
                            "package_digest",
                            "intake_id",
                            "artifact_digest",
                            "expected_alert_id",
                            "future_proof_body",
                        )
                    },
                },
            },
        )
        self.assertEqual(
            projected,
            {"subtype": "purported_complete_route", "subject": subject, **common},
        )
        exact_source = {
            "subtype": "purported_complete_route",
            **common,
            "subject": {
                **subject,
                "package_digest": "d" * 64,
                "artifact_digest": "e" * 64,
            },
        }
        self.assertEqual(
            semantic_owner_document("evidence", exact_source), exact_source
        )
        with self.assertRaisesRegex(
            WorkspaceIntegrityError, "evidence_subtype_invalid"
        ):
            source_qualification_document(
                "evidence",
                {
                    "subtype": "future_reserved_subtype",
                    "subject": _ExcludedBody(),
                    **common,
                },
            )

    def test_host_snapshot_is_bounded_current_cut_and_effect_free(self):
        before = self._stored_state()
        v4 = self.interface.reconstruct()
        with (
            mock.patch.object(
                self.store,
                "direct_recovery_read_scope",
                wraps=self.store.direct_recovery_read_scope,
            ) as scope,
            mock.patch.object(
                self.interface,
                "reconstruct",
                side_effect=AssertionError("reconstruct called"),
            ),
            mock.patch.object(
                self.store,
                "read_direct_recovery_facts",
                side_effect=AssertionError("full recovery facts called"),
            ),
            mock.patch.object(
                self.store,
                "list_mission_candidate_a1_bindings",
                side_effect=AssertionError("historical A1 inventory called"),
            ),
        ):
            wrapped = self.interface.host_snapshot()
        self.assertEqual(scope.call_count, 1)
        self.assertEqual(
            set(wrapped),
            {
                "schema_version",
                "authorization_cut",
                "current_state",
                "executive_orientation",
            },
        )
        self.assertEqual(
            wrapped["schema_version"],
            "mathematical_research.mission_host_snapshot.v4",
        )
        self.assertEqual(
            wrapped["authorization_cut"]["project_commit"],
            wrapped["current_state"]["observed_project_commit"],
        )
        self.assertEqual(
            wrapped["current_state"]["observed_project_commit"],
            v4["observed_project_commit"],
        )
        self.assertEqual(
            wrapped["current_state"]["latest_executive_epoch"],
            v4["latest_executive_epoch"],
        )
        self.assertEqual(self._stored_state(), before)
        self.assertFalse(hasattr(self.interface, "_direct_frontier_panorama_items"))
        with (
            mock.patch.object(
                self.interface,
                "_direct_frontier_panorama_items",
                side_effect=AssertionError("panorama called"),
                create=True,
            ),
            mock.patch.object(
                self.interface,
                "reconstruct",
                side_effect=AssertionError("reconstruct called"),
            ),
        ):
            self.assertEqual(
                self.interface.executive_orientation(), wrapped["executive_orientation"]
            )

    def test_genesis_complete_ordered_strategy_no_false_deltas(self):
        orientation = self.interface.executive_orientation()
        strategy = read_mission_strategy_head(
            self.store, mission_id=self.interface.mission_id
        )
        expected = semantic_owner_document("strategy", strategy.record.document)
        actual = {
            key: value
            for key, value in orientation["current_strategy"].items()
            if key not in {"handle", "formal_requests"}
        }
        self.assertEqual(actual, expected)
        expected_fields = {
            "mission_continuation",
            "integrated_comparison",
            "causal_inputs",
            "serious_opportunities",
            "selected_bets",
            "attention_actions",
            "context_treatment",
            "creativity_treatment",
            "reconsideration_conditions",
            "reversal_conditions",
            "revival_conditions",
            "owner_refs",
        }
        self.assertEqual(set(actual), expected_fields)

        def selected_source_value(value):
            if isinstance(value, dict):
                if set(value) == {"kind", "identity", "revision", "payload_sha256"}:
                    return {
                        "id": f"{value['kind']}:{value['identity']}",
                        "revision": value["revision"],
                    }
                return {key: selected_source_value(item) for key, item in value.items()}
            if isinstance(value, list):
                return [selected_source_value(item) for item in value]
            return value

        source = deep_thaw(strategy.record.document)
        for field in expected_fields:
            self.assertEqual(actual[field], selected_source_value(source[field]), field)
        self.assertEqual(
            orientation["continuity"]["mission_strategy_changes_since_checkpoint"],
            {"state": "no_checkpoint", "counts_by_kind": {}, "retrieve_call": None},
        )
        self.assertEqual(orientation["target"]["canonical_status"], "open")
        self.assertEqual(
            orientation["mission"]["purpose"], self.genesis_seed["mission"]["purpose"]
        )

    def test_historical_strategy_reference_remains_exact_and_deliberately_readable(self):
        epoch = self._epoch()
        old = read_mission_strategy_head(
            self.store, mission_id=self.interface.mission_id
        )
        mission_ref, _ = self.interface._resolve_owner_selector(
            {"id": "mission:mission.1"}
        )
        current = self._strategy(
            epoch,
            causal_inputs=[
                {
                    "source_ref": deep_thaw(old.to_reference()),
                    "decision_consequence": "Retain this earlier comparison exactly.",
                }
            ],
            serious_opportunities=[],
            selected_bets=[],
            attention_actions=[],
            context_treatment=[],
            creativity_treatment=[],
            reconsideration_conditions=[],
            reversal_conditions=[],
            revival_conditions=[],
            owner_refs=[mission_ref],
        )
        orientation = self.interface.executive_orientation()
        self.assertNotIn("strategy_ground", orientation)
        self.assertEqual(
            orientation["current_strategy"]["causal_inputs"][0]["source_ref"],
            {"id": "strategy:strategy.theta.1", "revision": old.revision},
        )
        exact = self._execute("retrieve", {
            "mode": "read", "purpose": "Recover the exact earlier comparison.",
            "ids": [f"strategy:strategy.theta.1@{old.revision}"],
        }, executive_epoch_id=epoch)
        self.assertEqual(exact["status"], "completed", exact)
        readable = json.loads(exact["result"]["items"][0]["readable_content"])
        self.assertEqual(readable["integrated_comparison"],
                         old.record.document["integrated_comparison"])
        self.assertTrue(readable["owner_refs"])
        self.assertEqual(orientation["current_strategy"]["handle"],
                         f"strategy:strategy.theta.1@{current.revision}")

    def test_unrelated_large_candidate_cannot_change_genesis_orientation(self):
        epoch = self._epoch()
        before = self.interface.executive_orientation()
        response = self._execute(
            "record_candidate",
            {
                "candidate_id": "candidate:candidate.unrelated",
                "proposal_kind": "lemma",
                "exact_statement": "Unselected local material " + "z" * 100000,
                "standing": {"status": "open", "basis": "Unrelated local test."},
            },
            executive_epoch_id=epoch,
        )
        self.assertEqual(response["status"], "completed", response)
        self.assertEqual(self.interface.executive_orientation(), before)

    def test_exact_caveats_survive_and_unincorporated_evidence_cannot_self_select(
        self,
    ):
        epoch = self._epoch()
        argument = [
            {
                "edge_id": "edge.local",
                "edge_kind": "argument",
                "premises": ["Only the bounded fixture premise."],
                "conclusion": "Exact retained local proof body.",
                "authority": {"class": "candidate_only"},
            }
        ]
        candidate = {
            "candidate_id": "candidate:candidate.caveats",
            "proposal_kind": "lemma",
            "exact_statement": "The bounded fixture remains conditional.",
            "standing": {"status": "open", "basis": "Not admitted mathematics."},
            "argument_edges": argument,
            "limitations": ["Bounded parameter only."],
            "non_inferences": ["No uniform or complete result follows."],
            "gaps": ["The endpoint remains unresolved."],
            "objections": ["Endpoint control may fail."],
        }
        from research_core.candidate_revision import (
            prepare_candidate_revision_v4,
            commit_candidate_revision,
        )

        # Existing owner writer creates retained proof data. Root exact reading is
        # exercised below; this fixture does not claim to fix root edge-authoring.
        authority = self.interface._direct_epoch_authority(epoch)
        prepared = prepare_candidate_revision_v4(
            authority=authority, **{**candidate, "candidate_id": "candidate.caveats"}
        )
        commit_candidate_revision(
            self.store,
            authority=authority,
            record=prepared,
            lease=self.interface._writer_lease(),
            actor="orientation-test",
        )
        branch = {
            "branch_id": "branch:branch.caveats",
            "question": "Does the endpoint survive?",
            "leverage_fingerprint": "bounded-endpoint",
            "target_hook": "Only this local implication.",
            "scoped_failures": [
                {
                    "scope": "One endpoint",
                    "finding": "That endpoint failed.",
                    "owner_refs": [
                        {"id": "candidate:candidate.caveats", "revision": 1}
                    ],
                }
            ],
            "non_exclusions": [
                {
                    "scope": "Interior parameters",
                    "statement": "The interior is not excluded.",
                    "owner_refs": [
                        {"id": "candidate:candidate.caveats", "revision": 1}
                    ],
                }
            ],
            "retained_residue": [
                {
                    "residue": "The interior estimate remains available.",
                    "owner_refs": [
                        {"id": "candidate:candidate.caveats", "revision": 1}
                    ],
                }
            ],
            "nonclaims": ["This is not a global rejection."],
        }
        result = self._execute("record_branch", branch, executive_epoch_id=epoch)
        self.assertEqual(result["status"], "completed", result)
        references = [
            self.interface._resolve_owner_selector({"id": selector})[0]
            for selector in (candidate["candidate_id"], branch["branch_id"])
        ]
        self._strategy(epoch, owner_refs=references)
        before = self.interface.executive_orientation()
        self.assertNotIn("strategy_ground", before)
        self.assertCountEqual(
            before["current_strategy"]["owner_refs"],
            [{"id": f"{ref['kind']}:{ref['identity']}", "revision": ref["revision"]}
             for ref in references],
        )
        exact = self._execute("retrieve", {
            "mode": "read", "purpose": "Inspect the exact retained argument and caveats.",
            "ids": ["candidate:candidate.caveats@1", "branch:branch.caveats@1"],
        }, executive_epoch_id=epoch)
        self.assertEqual(exact["status"], "completed", exact)
        candidate_read, branch_read = [
            json.loads(item["readable_content"]) for item in exact["result"]["items"]
        ]
        self.assertEqual(candidate_read["argument_edges"], argument)
        for field in ("limitations", "non_inferences", "gaps", "objections"):
            self.assertEqual(candidate_read[field], candidate[field])
        for field in ("scoped_failures", "non_exclusions", "retained_residue", "nonclaims"):
            self.assertEqual(branch_read[field], branch[field])
        consequence = (
            "Self-declared consequence cannot select this Evidence into Strategy."
        )
        auxiliary = self._execute(
            "record_candidate",
            {
                "candidate_id": "candidate:candidate.caveat-auxiliary",
                "proposal_kind": "lemma",
                "exact_statement": "An unselected local auxiliary remains unproved.",
                "standing": {"status": "open", "basis": "Unselected fixture."},
            },
            executive_epoch_id=epoch,
        )
        self.assertEqual(auxiliary["status"], "completed", auxiliary)
        result = self._execute(
            "synthesize",
            {
                "relation_question": "Does the local Candidate survive the Branch objection?",
                "inputs": [
                    {"id": candidate["candidate_id"], "role": "local claim"},
                    {
                        "id": "candidate:candidate.caveat-auxiliary",
                        "role": "scoped auxiliary",
                    },
                ],
                "compatibility_analysis": "The same local scope is compared.",
                "derivation_or_incompatibility": "No complete result is implied.",
                "scope": "These two exact owner revisions only.",
                "strength": "heuristic",
                "edge_survival": "The interior remains open.",
                "consequences": [
                    {
                        "kind": "evidence",
                        "evidence_id": "evidence:evidence.self-select",
                        "statement": "The local objection does not settle every parameter.",
                        "scope": "Interior only.",
                        "strength": "heuristic",
                        "semantic_role": "non_exclusion",
                        "limitations": ["No endpoint result."],
                        "non_inferences": ["No complete result."],
                        "decision_consequence": consequence,
                    }
                ],
            },
            executive_epoch_id=epoch,
        )
        self.assertEqual(result["status"], "completed", result)
        self.assertEqual(
            result["result"]["records"][0]["record_id"], "evidence:evidence.self-select"
        )
        after = self.interface.executive_orientation()
        self.assertEqual(after, before)
        self.assertNotIn(consequence, json.dumps(after))

    def test_open_a1_proof_disproof_independent_of_strategy_and_withdrawal(self):
        epoch = self._epoch()
        for disposition in ("proof", "disproof"):
            data = {
                "candidate_id": f"candidate:candidate.{disposition}",
                "proposal_kind": "mathematical_statement",
                "exact_statement": f"Purported complete {disposition}.",
                "complete_target_claim": {
                    "target": "riemann_hypothesis",
                    "disposition": disposition,
                },
                "standing": {"status": "open", "basis": "Unverified fixture."},
            }
            response = self._execute("record_candidate", data, executive_epoch_id=epoch)
            self.assertEqual(response["status"], "completed", response)
            data["standing"] = {
                "status": "withdrawn",
                "basis": "Author withdrawal does not resolve the older exact claim.",
            }
            response = self._execute("record_candidate", data, executive_epoch_id=epoch)
            self.assertEqual(response["status"], "completed", response)
        orientation = self.interface.executive_orientation()
        claims = orientation["proof_attention"]["open_candidate_a1"]
        expected_open = {
            (item.candidate_id, item.candidate_revision, item.candidate_digest)
            for item in self.store.list_mission_candidate_a1_bindings("mission.1")
            if item.hold_lifecycle == "open"
        }
        self.assertEqual(
            {
                (
                    item["candidate_ref"]["id"].removeprefix("candidate:"),
                    item["candidate_ref"]["revision"],
                    item["candidate_ref"]["payload_sha256"],
                )
                for item in claims
            },
            expected_open,
        )
        self.assertTrue(
            {"proof", "disproof"}.issubset(
                {item["claim_disposition"] for item in claims}
            )
        )
        self.assertTrue(any(item["candidate_ref"]["revision"] == 1 for item in claims))
        self.assertTrue(all(item["stage"] == "awaiting_a1_review" for item in claims))
        self.assertEqual(orientation["target"]["canonical_status"], "open")
        self.assertIsNone(orientation["proof_attention"]["admitted_result"])
        exact = claims[0]["candidate_ref"]
        owner_ref = {
            "kind": "candidate",
            "identity": exact["id"].removeprefix("candidate:"),
            "revision": exact["revision"],
            "payload_sha256": exact["payload_sha256"],
        }
        self._strategy(epoch, owner_refs=[owner_ref])
        selected_orientation = self.interface.executive_orientation()
        self.assertNotIn("strategy_ground", selected_orientation)
        self.assertEqual(selected_orientation["proof_attention"], orientation["proof_attention"])
        self.assertIn(
            {"id": exact["id"], "revision": exact["revision"]},
            selected_orientation["current_strategy"]["owner_refs"],
        )

    def test_typed_projection_keeps_mathematical_json_and_context_caveats(self):
        quoted = '{"closure":"semantic closure","provenance":"mathematical provenance"}'
        context = {
            "purpose": quoted,
            "question": "Question",
            "known_omissions": [quoted],
            "restricted_uses": [quoted],
            "restrictions": [quoted],
            "independence_treatment": {"closure": quoted},
            "invalidation_conditions": [
                {
                    "reference": {
                        "kind": "branch",
                        "identity": "b",
                        "revision": 1,
                        "payload_sha256": "a" * 64,
                    },
                    "condition": quoted,
                }
            ],
        }
        projected = semantic_owner_document("context", context)
        self.assertEqual(projected["purpose"], quoted)
        for field in ("known_omissions", "restricted_uses", "restrictions"):
            self.assertEqual(projected[field], context[field])
        self.assertEqual(
            projected["independence_treatment"], context["independence_treatment"]
        )
        self.assertEqual(projected["invalidation_conditions"][0]["condition"], quoted)
        self.assertEqual(
            projected["invalidation_conditions"][0]["reference"],
            {"id": "branch:b", "revision": 1},
        )
        candidate = semantic_owner_document(
            "candidate",
            {
                "argument_edges": [
                    {
                        "edge_id": "e",
                        "conclusion": quoted,
                        "authority": {"class": "candidate_only"},
                    }
                ],
                "exact_statement": quoted,
                "supporting_refs": [{
                    "kind": "context",
                    "id": "context.selected",
                    "revision": 7,
                    "digest_sha256": "a" * 64,
                    "selection": {"mode": "treatments", "treatment_ids": ["qualified"]},
                }],
                "provenance": "private",
            },
        )
        self.assertEqual(candidate["argument_edges"][0]["conclusion"], quoted)
        self.assertNotIn("provenance", candidate)
        self.assertEqual(candidate["supporting_refs"], [{
            "id": "context:context.selected",
            "revision": 7,
            "selection": {"mode": "treatments", "treatment_ids": ["qualified"]},
        }])

    def test_strategy_cited_context_v2_keeps_large_inventories_exact_read_only(self):
        from research_core.context_revision import (
            commit_context_revision,
            issue_context_revision_v2,
            read_context_revision,
        )

        epoch = self._epoch()
        mission_ref, _ = self.interface._resolve_owner_selector(
            {"id": "mission:mission.1"}
        )
        references = [mission_ref]
        for index in range(32):
            candidate_id = f"candidate:context-source.{index:02}"
            result = self._execute(
                "record_candidate",
                {
                    "candidate_id": candidate_id,
                    "proposal_kind": "lemma",
                    "exact_statement": f"Fixture-only inventory source {index}.",
                    "standing": {
                        "status": "open",
                        "basis": "Not an established result.",
                    },
                },
                executive_epoch_id=epoch,
            )
            self.assertEqual(result["status"], "completed", result)
            references.append(
                self.interface._resolve_owner_selector({"id": candidate_id})[0]
            )

        scope = {
            "assignment_mode": "lifecycle_historian",
            "search_lens": "One exact decision only.",
            "source_families": ["candidates", "contexts"],
            "known_omissions": ["Unselected branches are not covered."],
            "coverage_limits": ["This is not an exhaustive historical judgment."],
        }
        expected_semantics = {
            "purpose": "Retain the exact Context caveats, not its historical inventory.",
            "question": "Which local dependency may change this decision?",
            "indispensable_ground": [
                {
                    "reference": {"id": "mission:mission.1", "revision": 1},
                    "why": "The exact Mission purpose bounds the decision.",
                }
            ],
            "known_omissions": [
                "A known omitted endpoint.",
                "Z unexamined material remains open.",
            ],
            "restricted_uses": ["Do not treat exposure as proof."],
            "restrictions": ["No public disclosure.", "Retain scoped uncertainty."],
            "independence_treatment": {
                "approach": "Construct independently before comparison.",
                "closure": {"meaning": "This quoted semantic object is not machinery."},
            },
            "invalidation_conditions": [
                {
                    "reference": {"id": "mission:mission.1", "revision": 1},
                    "condition": "Reassess only if this exact Mission purpose changes.",
                }
            ],
            "historical_advisory_scope": scope,
        }
        inventory_fields = (
            "owner_source_references",
            "immutable_historical_references",
            "untrusted_material_locators",
        )
        exact_reads = []
        prior = None
        for count in (5, 33):
            with self.subTest(inventory_count=count):
                selected = references[:count]
                record = issue_context_revision_v2(
                    authority=self.interface._direct_epoch_authority(epoch),
                    context_id="context.inventory",
                    **{
                        key: value
                        for key, value in expected_semantics.items()
                        if key
                        not in {"indispensable_ground", "invalidation_conditions"}
                    },
                    indispensable_ground=[
                        {
                            "reference": mission_ref,
                            "why": expected_semantics["indispensable_ground"][0]["why"],
                        }
                    ],
                    invalidation_conditions=[
                        {
                            "reference": mission_ref,
                            "condition": expected_semantics["invalidation_conditions"][0][
                                "condition"
                            ],
                        }
                    ],
                    owner_source_references=[
                        {
                            "reference": reference,
                            "retrieval": f"{reference['kind']}:{reference['identity']}@{reference['revision']} OWNER_INVENTORY_ONLY "
                            + "o" * 1024,
                            "provenance": "fixture-owned retained source metadata",
                        }
                        for reference in selected
                    ],
                    immutable_historical_references=[
                        {
                            "reference": reference,
                            "why": "HISTORICAL_INVENTORY_ONLY " + "h" * 1024,
                        }
                        for reference in selected
                    ],
                    untrusted_material_locators=[
                        {
                            "id": f"https://example.invalid/retained/{index}",
                            "why": "UNTRUSTED_INVENTORY_ONLY " + "u" * 1024,
                            "treatment": "untrusted_mathematical_material",
                        }
                        for index in range(count)
                    ],
                )
                commit_context_revision(
                    self.store,
                    authority=self.interface._direct_epoch_authority(epoch),
                    record=record,
                    lease=self.interface._writer_lease(),
                    actor="context-startup-regression",
                    expected_head_revision=None if prior is None else prior.revision,
                    expected_head_payload_digest=None
                    if prior is None
                    else prior.payload_digest,
                )
                prior = read_context_revision(
                    self.store, context_id="context.inventory"
                )
                self.assertEqual(prior.record.document["schema_version"], 2)
                context_ref, _ = self.interface._resolve_owner_selector(
                    {"id": "context:context.inventory"}
                )
                self._strategy(epoch, owner_refs=[context_ref])
                before_reads = self._stored_state()
                exact = self._execute(
                    "retrieve",
                    {
                        "mode": "read",
                        "purpose": "Deliberately inspect the complete exact Context inventories.",
                        "ids": [f"context:context.inventory@{prior.revision}"],
                    },
                    executive_epoch_id=epoch,
                )
                self.assertEqual(exact["status"], "completed", exact)
                item = exact["result"]["items"][0]
                self.assertEqual(item["status"], "readable")
                readable = json.loads(item["readable_content"])
                source = deep_thaw(prior.record.document)
                for field in inventory_fields:
                    self.assertEqual(len(readable[field]), count)
                expected_source_refs = [
                    {
                        "reference": {
                            "id": f"{row['reference']['kind']}:{row['reference']['identity']}",
                            "revision": row["reference"]["revision"],
                        },
                        "retrieval": row["retrieval"],
                    }
                    for row in source["owner_source_references"]
                ]
                expected_historical_refs = [
                    {
                        "reference": {
                            "id": f"{row['reference']['kind']}:{row['reference']['identity']}",
                            "revision": row["reference"]["revision"],
                        },
                        "why": row["why"],
                    }
                    for row in source["immutable_historical_references"]
                ]
                self.assertEqual(
                    readable["owner_source_references"], expected_source_refs
                )
                self.assertEqual(
                    readable["immutable_historical_references"],
                    expected_historical_refs,
                )
                self.assertEqual(
                    readable["untrusted_material_locators"],
                    source["untrusted_material_locators"],
                )
                self.assertEqual(
                    readable,
                    {
                        **expected_semantics,
                        "owner_source_references": expected_source_refs,
                        "immutable_historical_references": expected_historical_refs,
                        "untrusted_material_locators": source[
                            "untrusted_material_locators"
                        ],
                    },
                )
                exact_reads.append(item["readable_content"])
                orientation = self.interface.executive_orientation()
                self.assertNotIn("strategy_ground", orientation)
                self.assertIn(
                    {"id": "context:context.inventory", "revision": prior.revision},
                    orientation["current_strategy"]["owner_refs"],
                )
                for field in inventory_fields:
                    self.assertNotIn(field, json.dumps(orientation))
                for marker in (
                    "OWNER_INVENTORY_ONLY",
                    "HISTORICAL_INVENTORY_ONLY",
                    "UNTRUSTED_INVENTORY_ONLY",
                ):
                    self.assertNotIn(marker, json.dumps(orientation))
                self.assertEqual(self._stored_state(), before_reads)
        self.assertGreater(len(exact_reads[1]), len(exact_reads[0]) + 75000)
        before_retained_read = self._stored_state()
        retained = self._execute(
            "retrieve",
            {
                "mode": "read",
                "purpose": "Reopen the prior exact Context unchanged.",
                "ids": ["context:context.inventory@1"],
            },
            executive_epoch_id=epoch,
        )
        self.assertEqual(
            retained["result"]["items"][0]["readable_content"], exact_reads[0]
        )
        self.assertEqual(self._stored_state(), before_retained_read)

    def test_context_exact_and_whole_source_qualification_preserve_optional_fields(self):
        base = {
            "purpose": "One exact purpose",
            "question": "One exact question",
            "indispensable_ground": [],
            "owner_source_references": [],
            "immutable_historical_references": [],
            "untrusted_material_locators": [],
            "known_omissions": [],
            "restricted_uses": [],
            "restrictions": [],
            "independence_treatment": {},
            "invalidation_conditions": [],
        }
        for historical_scope in (None, {}, {"coverage_limits": []}):
            source = dict(base)
            if historical_scope is not None:
                source["historical_advisory_scope"] = historical_scope
            with self.subTest(historical_scope=historical_scope):
                self.assertEqual(semantic_owner_document("context", source), source)
                selection = {"mode": "whole_context"}
                self.assertEqual(
                    source_qualification_document("context", source, selection),
                    {"selection": selection, "context": source},
                )

    def test_failed_interval_preserves_earlier_output_and_redacts_unknown_text(self):
        first = self._epoch()
        self._capture_pending_material(first)
        self.interface.record_direct_failed_executive_epoch_from_owner(
            {
                "executiveEpochId": first,
                "reconciliation": {
                    "stage": "goal_runtime",
                    "failure_reason": "operator_stop",
                },
            }
        )
        second = self._authorize()
        self.interface.record_direct_failed_executive_epoch_from_owner(
            {
                "executiveEpochId": second,
                "reconciliation": {
                    "stage": "authorization_only",
                    "failure_reason": "private arbitrary failure text",
                },
            }
        )
        snapshot = self.interface.host_snapshot()
        self.assertNotIn("host_recovery_facts", snapshot)
        with self.store.direct_recovery_read_scope():
            facts = recovery_facts_in_snapshot(self.interface)
        self.assertEqual(
            [item["executive_epoch_id"] for item in facts["terminal_epochs"]],
            [first, second],
        )
        self.assertEqual(facts["failed_output_attention"]["capture_count"], 1)
        self.assertEqual(facts["failed_output_attention"]["artifact_count"], 1)
        self.assertEqual(
            facts["terminal_epochs"][1]["reconciliation"]["failure_reason"],
            "unclassified_failure",
        )
        self.assertNotIn("private arbitrary", json.dumps(facts))

    def test_failed_output_predicate_includes_late_origin_not_assignments_or_covered(
        self,
    ):
        base = dict(
            capture_kind="output",
            origin_commit=30,
            terminal_kind="failed_before_checkpoint",
            terminal_commit=5,
            baseline_commit=20,
            pending=True,
        )
        self.assertTrue(failed_output_is_relevant(**base))
        self.assertFalse(
            failed_output_is_relevant(**{**base, "capture_kind": "assignment"})
        )
        self.assertFalse(failed_output_is_relevant(**{**base, "pending": False}))
        self.assertFalse(failed_output_is_relevant(**{**base, "origin_commit": 10}))
        self.assertTrue(
            failed_output_is_relevant(
                **{**base, "origin_commit": 1, "baseline_commit": None}
            )
        )

    def test_canonical_status_requires_matching_admitted_binding(self):
        from types import SimpleNamespace

        for disposition in ("proved", "disproved"):
            admitted = {"disposition": disposition}
            snapshot = SimpleNamespace(
                state={
                    "project": {
                        "proof_status": disposition,
                        "admitted_result": admitted,
                    }
                }
            )
            self.assertEqual(_canonical_status(snapshot, admitted), disposition)
            with self.assertRaises(WorkspaceIntegrityError):
                _canonical_status(snapshot, None)
        with self.assertRaises(WorkspaceIntegrityError):
            _canonical_status(
                SimpleNamespace(state={"project": {"proof_status": "incomplete"}}),
                {"disposition": "proved"},
            )

    def test_canonical_source_mismatch_denied_without_store_mutation(self):
        before = self._stored_state()
        with mock.patch(
            "research_core.executive_orientation.verify_snapshot_current",
            return_value=object(),
        ):
            with self.assertRaisesRegex(
                WorkspaceIntegrityError, "canonical_binding_mismatch"
            ):
                self.interface.executive_orientation()
        self.assertEqual(self._stored_state(), before)

    def test_reserved_evidence_projection_keeps_judgment_not_grant_machinery(self):
        document = {
            "subtype": "complete_claim_admission_review",
            "subject": {
                "mission_id": "mission.1",
                "candidate_ref": {
                    "mission_id": "mission.1",
                    "candidate_id": "candidate.exact",
                    "revision": 1,
                    "digest_sha256": "a" * 64,
                },
                "case_ref": {
                    "evidence_id": "evidence.case",
                    "revision": 1,
                    "payload_sha256": "b" * 64,
                },
                "reviewer": {
                    "grant_id": "private-grant",
                    "grant_digest_sha256": "c" * 64,
                },
                "disposition": "material_objection",
                "review_finding": "The named implication omits a case.",
                "objections": [
                    {
                        "exact_objection": "Missing boundary",
                        "affected_scope": "Final implication",
                        "materiality_basis": "Completeness does not follow.",
                    }
                ],
                "cited_basis": [],
                "canonical_effect": "none",
            },
            "exact_scope": "Exact frozen Candidate",
            "rigor": "review",
            "limitations": ["Scope-limited."],
            "non_inferences": ["No canonical effect."],
        }
        projected = semantic_owner_document("evidence", document)
        self.assertEqual(
            projected["subject"]["review_finding"],
            document["subject"]["review_finding"],
        )
        self.assertEqual(
            projected["subject"]["objections"], document["subject"]["objections"]
        )
        self.assertEqual(
            projected["subject"]["candidate_ref"],
            {"id": "candidate:candidate.exact", "revision": 1},
        )
        self.assertNotIn("private-grant", json.dumps(projected))
        self.assertNotIn("reviewer", projected["subject"])
        # The private exact-read sidecar must not reinterpret reserved source
        # machinery as ordinary Evidence capture scopes.
        with mock.patch.object(self.store, "read_evidence_meaning_revision", return_value={
            "record": document,
            "payload_digest": hashlib.sha256(canonical_json_bytes(document)).hexdigest(),
            "sources": _ExcludedBody(),
        }):
            owner = self.interface._read_recovery_owner_revision(
                kind="evidence", identity="evidence.reserved-review", revision=1,
                include_evidence_sources=True,
            )
        self.assertNotIn("evidence_sources", owner)
        self.assertEqual(semantic_owner_document("evidence", owner["document"]), projected)

    def test_historical_mission_keeps_selected_purpose_after_fence(self):
        epoch = self._epoch()
        mission_ref, _ = self.interface._resolve_owner_selector(
            {"id": "mission:mission.1"}
        )
        self._strategy(epoch, owner_refs=[mission_ref])
        self.interface.fence_mission_from_owner(
            {
                "threadId": self.goal_thread_id,
                "reason": "shared_authority_loss",
                "containmentScope": "mission_fence",
            },
            executive_epoch_id=epoch,
        )
        orientation = self.interface.executive_orientation()
        self.assertNotIn("strategy_ground", orientation)
        self.assertIn(
            {"id": "mission:mission.1", "revision": 1},
            orientation["current_strategy"]["owner_refs"],
        )
        self.assertFalse(orientation["mission"]["effective"])
        from research_core.mission_retrieval import _exact_read

        with self.store.direct_recovery_read_scope():
            selected = _exact_read(self.interface, "mission:mission.1@1")
        self.assertEqual(selected["status"], "readable")
        historical = json.loads(selected["readable_content"])
        self.assertTrue(historical["effective"])
        self.assertEqual(historical["purpose"], self.genesis_seed["mission"]["purpose"])
        # This is owner-read addressability, not root authorization after fence.

    def test_postcut_nonroot_change_is_omitted_until_deliberate_retrieval(self):
        epoch = self._epoch()
        self._strategy(
            epoch,
            integrated_comparison="One truthful decision preserved for the successor.",
        )
        result = self._execute("checkpoint", {}, executive_epoch_id=epoch)
        self.assertEqual(result["status"], "completed", result)
        next_epoch = self._authorize()
        self.goal_thread_id += ":next"
        self._bind(next_epoch)
        before = self.interface.executive_orientation()
        result = self._execute(
            "record_candidate",
            {
                "candidate_id": "candidate:candidate.postcut",
                "proposal_kind": "lemma",
                "exact_statement": "postcut-private-mathematics",
                "standing": {"status": "open", "basis": "Unselected."},
            },
            executive_epoch_id=next_epoch,
        )
        self.assertEqual(result["status"], "completed", result)
        with (
            mock.patch.object(
                self.interface,
                "reconstruct",
                side_effect=AssertionError("startup reconstruction called"),
            ),
            mock.patch.object(
                self.store,
                "read_direct_recovery_facts",
                side_effect=AssertionError("startup history materialization called"),
            ),
            mock.patch.object(
                self.store,
                "read_root_change_page",
                side_effect=AssertionError("startup change traversal called"),
            ),
        ):
            orientation = self.interface.host_snapshot()["executive_orientation"]
        self.assertEqual(orientation["current_strategy"], before["current_strategy"])
        self.assertEqual(orientation["scientific_context"], before["scientific_context"])
        self.assertEqual(
            orientation["continuity"]["mission_strategy_changes_since_checkpoint"],
            {"state": "none", "counts_by_kind": {}, "retrieve_call": None},
        )
        self.assertNotIn("owner_changes_since_checkpoint", orientation["continuity"])
        self.assertNotIn("postcut-private-mathematics", json.dumps(orientation))
        changes = self._execute(
            "retrieve",
            {
                "mode": "changes_since_checkpoint",
                "purpose": "Read the exact omitted post-checkpoint change.",
            },
            executive_epoch_id=next_epoch,
        )
        self.assertEqual(changes["status"], "completed", changes)
        candidate = next(
            item
            for item in changes["result"]["items"]
            if item["kind"] == "candidate"
        )
        self.assertEqual(candidate["changes"], ["new"])

    def test_formal_attention_tracks_only_current_exact_request(self):
        from research_core.formal_session import (
            discover_formal_requests,
            prepare_formal_session_creation,
            commit_formal_session_creation,
            _issue_verified_formal_session_result,
            prepare_formal_session_terminal_revision,
            commit_formal_session_terminal_revision,
        )

        epoch = self._epoch()
        context = self._execute(
            "record_context",
            {
                "context_id": "context:context.formal-orientation",
                "subject": "Frozen formal input.",
                "question": "Does the construction work?",
                "material": [{"id": "mission:mission.1", "why": "Mission scope."}],
            },
            executive_epoch_id=epoch,
        )
        self.assertEqual(context["status"], "completed", context)
        context_ref, _ = self.interface._resolve_owner_selector(
            {"id": "context:context.formal-orientation"}
        )
        strategy = self._strategy(
            epoch,
            selected_bets=[
                {
                    "bet": "Exact formal test",
                    "discriminator": "Exact result",
                    "owner_refs": [],
                    "formal_request": {
                        "purpose": "targeted_verification",
                        "context_ref": context_ref,
                    },
                }
            ],
        )
        self.assertEqual(
            self.interface.executive_orientation()["formal_attention"][0][
                "session_state"
            ],
            "not_started",
        )
        authority = self.interface._direct_epoch_authority(epoch)
        prepared = prepare_formal_session_creation(
            self.store,
            authority=authority,
            formal_request=discover_formal_requests(strategy)[0],
        )
        commit_formal_session_creation(
            self.store,
            authority=authority,
            prepared=prepared,
            lease=self.interface._writer_lease(),
            actor="orientation-test",
        )
        attention = self.interface.executive_orientation()["formal_attention"]
        self.assertEqual(attention[0]["session_state"], "open")
        self.assertIsNone(attention[0]["terminal"])
        reads = self._execute(
            "retrieve",
            {
                "mode": "read",
                "purpose": "Read exact current owners.",
                "ids": [attention[0]["session_handle"], "mission:mission.1@1"],
            },
            executive_epoch_id=epoch,
        )
        self.assertEqual(reads["status"], "completed", reads)
        self.assertEqual(
            [item["status"] for item in reads["result"]["items"]],
            ["readable", "readable"],
        )
        self._strategy(
            epoch,
            integrated_comparison="New current Strategy; old Session remains retained.",
        )
        new_attention = self.interface.executive_orientation()["formal_attention"]
        self.assertEqual(new_attention[0]["session_state"], "not_started")
        self.assertIsNone(new_attention[0]["session_handle"])
        new_strategy = read_mission_strategy_head(
            self.store, mission_id=self.interface.mission_id
        )
        authority = self.interface._direct_epoch_authority(epoch)
        new_session = prepare_formal_session_creation(
            self.store,
            authority=authority,
            formal_request=discover_formal_requests(new_strategy)[0],
        )
        commit_formal_session_creation(
            self.store,
            authority=authority,
            prepared=new_session,
            lease=self.interface._writer_lease(),
            actor="orientation-test",
        )
        from research_core.mission_evidence import (
            RawCaptureArtifactInput,
            prepare_raw_capture,
            commit_raw_capture,
        )

        attempt_id = "attempt.fixture"
        result_digest = "a" * 64
        session_id = str(new_session.record.document["session_id"])
        observation_id = "formal-attempt-result:" + hashlib.sha256(
            canonical_json_bytes(
                {
                    "domain": "mathematical_research.formal_attempt_observation.v1",
                    "attempt_id": attempt_id,
                    "result_digest_sha256": result_digest,
                }
            )
        ).hexdigest()
        capture = prepare_raw_capture(
            self.store,
            mission_id="mission.1",
            executive_epoch_id=epoch,
            capture_kind="output",
            observation_id=observation_id,
            assignment_id=session_id,
            provenance={
                "kind": "workstation_attempt_result",
                "attempt_id": attempt_id,
                "result_digest_sha256": result_digest,
                "session_id": session_id,
                "session_digest_sha256": new_session.record.payload_sha256,
            },
            completion={
                "attempt_state": "failed",
                "provider_effect_certainty": "known",
            },
            artifacts=(
                RawCaptureArtifactInput(
                    role="result",
                    logical_name="formal.txt",
                    content_bytes=b"Retained formal result.",
                    media_type="text/plain",
                    encoding="utf-8",
                ),
            ),
        )
        commit_raw_capture(
            self.store,
            cas=self.cas,
            record=capture,
            lease=self.interface._writer_lease(),
            actor="orientation-test",
        )
        terminal = prepare_formal_session_terminal_revision(
            self.store,
            authority=authority,
            verified_result=_issue_verified_formal_session_result(
                session_id=session_id,
                attempt_id=attempt_id,
                attempt_state="failed",
                result_digest_sha256=result_digest,
                raw_capture_id=capture.capture_id,
                raw_capture_digest_sha256=capture.digest_sha256,
            ),
        )
        commit_formal_session_terminal_revision(
            self.store,
            authority=authority,
            prepared=terminal,
            lease=self.interface._writer_lease(),
            actor="orientation-test",
        )
        state_before_projection = self._stored_state()
        settled = self.interface.executive_orientation()["formal_attention"][0]
        self.assertEqual(self._stored_state(), state_before_projection)
        self.assertEqual(settled["session_state"], "terminal")
        self.assertEqual(
            settled["terminal"],
            {
                "attempt_id": "attempt.fixture",
                "attempt_state": "failed",
                "raw_capture_handle": f"capture:{capture.capture_id}",
            },
        )
        reads = self._execute(
            "retrieve",
            {
                "mode": "read",
                "purpose": "Compare retained and current exact Sessions.",
                "ids": [
                    attention[0]["session_handle"],
                    settled["session_handle"],
                    settled["session_handle"].rsplit("@", 1)[0] + "@1",
                ],
            },
            executive_epoch_id=epoch,
        )
        self.assertEqual(reads["status"], "completed", reads)
        self.assertEqual(
            [
                json.loads(item["readable_content"])["lifecycle"]
                for item in reads["result"]["items"]
            ],
            ["open", "terminal", "open"],
        )
        exact_owners = [
            self.interface._read_recovery_owner_revision(
                kind="session",
                identity=new_session.record.document["session_id"],
                revision=2,
            ),
        ]
        self._strategy(epoch, owner_refs=[owner["reference"] for owner in exact_owners])
        # Exact retained owners can be authored into Strategy by the existing
        # owner API; no new root write-selector permission is asserted here.
        selected_orientation = self.interface.executive_orientation()
        self.assertNotIn("strategy_ground", selected_orientation)
        self.assertEqual(
            selected_orientation["current_strategy"]["owner_refs"],
            [{"id": f"session:{new_session.record.document['session_id']}", "revision": 2}],
        )
        terminal_read = json.loads(reads["result"]["items"][1]["readable_content"])
        self.assertEqual(
            terminal_read["terminal_binding"]["raw_capture_handle"],
            f"capture:{capture.capture_id}",
        )

    def test_late_output_from_precheckpoint_failure_remains_attentive(self):
        first = self._epoch()
        self._capture_pending_material(first)
        self.interface.record_direct_failed_executive_epoch_from_owner(
            {
                "executiveEpochId": first,
                "reconciliation": {
                    "stage": "goal_runtime",
                    "failure_reason": "boundary_failure",
                },
            }
        )
        second = self._authorize()
        self.goal_thread_id += ":successor"
        self._bind(second)
        self._strategy(
            second, integrated_comparison="Retain a truthful successor decision."
        )
        self.assertEqual(
            self._execute("checkpoint", {}, executive_epoch_id=second)["status"],
            "completed",
        )
        self.assertNotIn("host_recovery_facts", self.interface.host_snapshot())
        with self.store.direct_recovery_read_scope():
            initial_facts = recovery_facts_in_snapshot(self.interface)
        self.assertEqual(initial_facts["failed_output_attention"]["state"], "none")
        late = direct_fixture.prepare_raw_capture(
            self.store,
            mission_id=self.interface.mission_id,
            executive_epoch_id=first,
            capture_kind="output",
            observation_id="late-after-new-checkpoint",
            assignment_id="child:late",
            provenance={"kind": "orientation_fixture"},
            completion={"lifecycle": "completed"},
            artifacts=(
                direct_fixture.RawCaptureArtifactInput(
                    role="result",
                    logical_name="late.txt",
                    content_bytes=b"Late retained fixture.",
                    media_type="text/plain",
                    encoding="utf-8",
                ),
            ),
        )
        direct_fixture.commit_raw_capture(
            self.store,
            cas=self.cas,
            record=late,
            lease=self.interface._writer_lease(),
            actor="orientation-test",
        )
        with self.store.direct_recovery_read_scope():
            facts = recovery_facts_in_snapshot(self.interface)
        self.assertEqual(facts["failed_output_attention"]["capture_count"], 1)
        self.assertEqual(facts["failed_output_attention"]["artifact_count"], 1)
        origin = next(
            item
            for item in facts["terminal_epochs"]
            if item["executive_epoch_id"] == first
        )
        self.assertLess(origin["project_commit"], facts["checkpoint_project_commit"])

    def test_retained_legacy_a1_remains_direction_unspecified(self):
        # Reuse the established migration-fixture constructor; its only bypass
        # materializes retained v1 data, never a new current v1 writer route.
        direct_fixture.DirectMissionInterfaceTests.test_migrated_candidate_v1_a1_is_recoverable_by_exact_revision(
            self
        )
        attention = self.interface.executive_orientation()["proof_attention"][
            "open_candidate_a1"
        ]
        self.assertEqual(len(attention), 1)
        self.assertEqual(attention[0]["claim_disposition"], "legacy_unspecified")
        self.assertEqual(attention[0]["candidate_ref"]["revision"], 1)

    def test_real_canonical_rebind_resolves_a1_and_projects_closeout(self):
        from research_core.workspace_store import WorkspaceStore

        original = WorkspaceStore.rebind_admitted_result
        snapshots = []

        def remember_snapshot(store, **kwargs):
            outcome = original(store, **kwargs)
            snapshots.append(kwargs["target_canonical_snapshot"])
            return outcome

        # Reuse the existing owner-issued Case/Review/Decision + isolated
        # canonical writer/rebind fixture instead of creating another writer.
        with mock.patch.object(
            WorkspaceStore, "rebind_admitted_result", remember_snapshot
        ):
            direct_fixture.DirectMissionInterfaceTests.test_candidate_a1_review_grant_is_transitive_bounded_and_replayable(
                self
            )
        self.assertTrue(snapshots)
        self.interface._canonical_snapshot = snapshots[-1]
        before = self._stored_state()
        orientation = self.interface.executive_orientation()
        self.assertEqual(orientation["proof_attention"]["open_candidate_a1"], [])
        admitted = orientation["proof_attention"]["admitted_result"]
        self.assertEqual(
            orientation["target"]["canonical_status"], admitted["disposition"]
        )
        self.assertIsNone(
            orientation["continuity"]["mission_strategy_changes_since_checkpoint"]["retrieve_call"]
        )
        self.assertIsNone(
            orientation["continuity"]["checkpoint_attention"]["retrieve_call"]
        )
        self.assertEqual(
            orientation["retrieval"],
            {
                "usage_call": {
                    "operation": "usage",
                    "input": {"for_operation": "checkpoint"},
                },
                "available_modes": [],
                "recommended_calls": [],
            },
        )
        self.assertEqual(self._stored_state(), before)

    def test_persisted_admission_stages_and_rejection_preserve_exact_claim(self):
        for disposition in ("authorize_exact_delta", "reject"):
            with self.subTest(disposition=disposition):
                if disposition == "reject":
                    # A separate disposable fixture prevents duplicate Case identities.
                    self.setUp()
                epoch = self._epoch()
                candidate_id = "candidate.admission-orientation"
                result = self._execute(
                    "record_candidate",
                    {
                        "candidate_id": f"candidate:{candidate_id}",
                        "proposal_kind": "mathematical_statement",
                        "exact_statement": "Purported complete RH fixture.",
                        "complete_target_claim": {
                            "target": "riemann_hypothesis",
                            "disposition": "proof",
                        },
                        "standing": {
                            "status": "open",
                            "basis": "Unverified exact fixture.",
                        },
                    },
                    executive_epoch_id=epoch,
                )
                self.assertEqual(result["status"], "completed", result)
                ref = result["result"]["open_candidate_a1"]["candidate_ref"]
                selector = {
                    "id": f"candidate:{candidate_id}",
                    "revision": ref["revision"],
                    "payload_sha256": ref["payload_sha256"],
                }
                context = self._execute(
                    "record_context",
                    {
                        "context_id": "context:context.admission-orientation",
                        "subject": "Exact proof review",
                        "question": "Does it hold?",
                        "material": [
                            {"id": f"candidate:{candidate_id}", "why": "Exact claim."}
                        ],
                    },
                    executive_epoch_id=epoch,
                )
                self.assertEqual(context["status"], "completed", context)
                context_ref = {
                    "id": "context:context.admission-orientation",
                    "revision": 1,
                }

                def binding(child):
                    return {
                        "project_id": "project.rh",
                        "mission_id": "mission.1",
                        "executive_epoch_id": epoch,
                        "root_thread_id": self.goal_thread_id,
                        "actual_parent_thread_id": self.goal_thread_id,
                        "actual_child_thread_id": child,
                        "actual_depth": 1,
                    }

                def stage():
                    before = self._stored_state()
                    attention = self.interface.executive_orientation()[
                        "proof_attention"
                    ]
                    self.assertEqual(self._stored_state(), before)
                    return attention["open_candidate_a1"][0]["stage"]

                self.assertEqual(stage(), "awaiting_a1_review")
                grant = self.interface.issue_candidate_a1_review_grant(
                    {
                        "child_thread_id": "child:triage",
                        "assignment": "Independent exact triage",
                        "context": context_ref,
                        "candidate_ref": selector,
                    },
                    binding=binding("child:triage"),
                )
                self.interface.execute_candidate_a1_review(
                    {
                        "mode": "submit",
                        "disposition": "admission_ready",
                        "review_finding": "No material objection at triage.",
                        "no_remaining_material_objection": True,
                    },
                    grant=grant,
                    binding=binding("child:triage"),
                )
                self.assertEqual(stage(), "awaiting_admission_case")
                case = self.interface.open_complete_claim_admission_case(
                    {"candidate_ref": selector}, executive_epoch_id=epoch
                )
                self.assertEqual(stage(), "awaiting_admission_review")
                grant_request = {
                    "assignment": "Role-disjoint independent review",
                    "context": context_ref,
                    "case_ref": case["case_ref"],
                }
                review_grant = self.interface.issue_admission_review_grant(
                    {**grant_request, "child_thread_id": "child:review"},
                    binding=binding("child:review"),
                )
                cited = [
                    {
                        "kind": "candidate",
                        "identity": candidate_id,
                        "revision": ref["revision"],
                        "payload_sha256": ref["payload_sha256"],
                    }
                ]
                self.interface.execute_admission_review(
                    {
                        "mode": "submit",
                        "disposition": "no_material_objection",
                        "review_finding": "No exact material objection.",
                        "cited_basis": cited,
                    },
                    grant=review_grant,
                    binding=binding("child:review"),
                )
                self.assertEqual(stage(), "awaiting_admission_decision")
                decision_grant = self.interface.issue_admission_decision_grant(
                    {**grant_request, "child_thread_id": "child:admitter"},
                    binding=binding("child:admitter"),
                )
                decision_request = {
                    "mode": "submit",
                    "disposition": disposition,
                    "decision_basis": "Exact Case and independent Review.",
                }
                if disposition == "reject":
                    decision_request.update(
                        {
                            "objections": [
                                {
                                    "exact_objection": "One terminal case is omitted.",
                                    "affected_scope": "Complete claim.",
                                    "materiality_basis": "The missing implication defeats completeness.",
                                }
                            ],
                            "cited_basis": cited,
                        }
                    )
                self.interface.execute_admission_decision(
                    decision_request,
                    grant=decision_grant,
                    binding=binding("child:admitter"),
                )
                if disposition == "reject":
                    binding_state = next(
                        item
                        for item in self.store.list_mission_candidate_a1_bindings(
                            "mission.1"
                        )
                        if item.candidate_id == candidate_id
                    )
                    self.assertEqual(binding_state.hold_lifecycle, "resolved")
                    self.assertEqual(
                        binding_state.admission_rejection["disposition"],
                        "independently_invalidated",
                    )
                    self.assertEqual(
                        self.interface.executive_orientation()["proof_attention"][
                            "open_candidate_a1"
                        ],
                        [],
                    )
                else:
                    self.assertEqual(stage(), "awaiting_external_canonical_rebind")
                self.assertEqual(
                    self.interface.executive_orientation()["target"][
                        "canonical_status"
                    ],
                    "open",
                )


if __name__ == "__main__":
    unittest.main()
