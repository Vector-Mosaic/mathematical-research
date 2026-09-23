"""Focused Context-led orientation projection and exact-source fixtures."""

from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path
from unittest import mock

PACKAGE_ROOT = Path(__file__).resolve().parents[1]
TEST_ROOT = Path(__file__).resolve().parent
for location in (PACKAGE_ROOT, TEST_ROOT):
    if str(location) not in sys.path:
        sys.path.insert(0, str(location))

import test_mission_interface_direct as direct_fixture
from research_core.context_revision import issue_context_revision_v3, read_context_revision
from research_core.executive_orientation import scientific_context_in_snapshot
from research_core.mission_frontier import (
    read_mission_strategy_head,
    prepare_strategy_revision,
    commit_strategy_revision,
)
from research_core.mission_operation_contract import validate_executive_orientation
from research_core.research_model import deep_thaw


class ScientificOrientationV3Tests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        direct_fixture.DirectMissionInterfaceTests.setUpClass.__func__(cls)

    setUp = direct_fixture.DirectMissionInterfaceTests.setUp
    _request = staticmethod(direct_fixture.DirectMissionInterfaceTests._request)
    _authorize = direct_fixture.DirectMissionInterfaceTests._authorize
    _bind = direct_fixture.DirectMissionInterfaceTests._bind
    _execute = direct_fixture.DirectMissionInterfaceTests._execute
    _linux_principal = staticmethod(direct_fixture.DirectMissionInterfaceTests._linux_principal)

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

    @staticmethod
    def _context_ref(value):
        return {
            "kind": "context",
            "identity": value.record.document["context_id"],
            "revision": value.revision,
            "payload_sha256": value.payload_digest,
        }

    def test_strategy_reference_does_not_read_or_expose_owner_body(self):
        epoch = self._epoch()
        context = self._scientific_context(epoch, "scientific.selected", {
            "selected": self._scientific_treatment("SELECTED OWNER BODY"),
            "unselected": self._scientific_treatment("UNSELECTED SIBLING BODY"),
        })
        self._strategy(epoch, owner_refs=[self._context_ref(context)])
        with mock.patch.object(
            self.interface, "_read_current_strategy_selected_owner",
            side_effect=AssertionError("orientation expanded Strategy ground"),
        ):
            orientation = deep_thaw(self.interface.executive_orientation())
        self.assertEqual(orientation["schema_version"], "mathematical_research.executive_orientation.v3")
        self.assertNotIn("strategy_ground", orientation)
        self.assertEqual(
            orientation["current_strategy"]["owner_refs"],
            [{"id": "context:scientific.selected", "revision": context.revision}],
        )
        self.assertNotIn("SELECTED OWNER BODY", json.dumps(orientation))
        self.assertNotIn("UNSELECTED SIBLING BODY", json.dumps(orientation))
        self.assertIn("proof_attention", orientation)
        self.assertIn("formal_attention", orientation)
        validate_executive_orientation(orientation)

    def test_selected_science_preserves_qualifications_and_exact_correction_reuse(self):
        epoch = self._epoch()
        source1 = self._scientific_context(epoch, "scientific.source", {
            "selected": self._scientific_treatment("old selected"),
            "sibling": self._scientific_treatment("old sibling"),
        })
        source2 = self._scientific_context(epoch, "scientific.source", {
            "selected": self._scientific_treatment("second selected"),
            "sibling": self._scientific_treatment("second sibling"),
        }, previous=source1)
        source3 = self._scientific_context(epoch, "scientific.source", {
            "selected": self._scientific_treatment(
                "current selected", qualifications=["finite domain only"]),
            "sibling": self._scientific_treatment("current sibling"),
            "unrelated": self._scientific_treatment("UNRELATED NEW BODY"),
        }, previous=source2, metadata={
            "known_omissions": ["no growing-domain result"],
            "restrictions": ["exact normalization required"],
        })
        selected = {"mode": "treatments", "treatment_ids": ["selected"]}
        sibling = {"mode": "treatments", "treatment_ids": ["sibling"]}
        self._scientific_context(epoch, "scientific.root", {
            "first": self._scientific_treatment("first exact historical use", sources=[
                self._scientific_source(self._context_ref(source1), selection=selected)]),
            "second": self._scientific_treatment("second exact historical use", sources=[
                self._scientific_source(self._context_ref(source2), selection=selected)]),
            "other_selection": self._scientific_treatment("distinct selected use", sources=[
                self._scientific_source(self._context_ref(source1), selection=sibling)]),
        }, exposed=[{"context_id": "scientific.source", "treatment_id": "selected"}])
        science = self._scientific_projection("scientific.root")
        self.assertEqual(science["state"], "available")
        deeper = science["treatments"][-1]
        self.assertEqual(deeper["content"]["account"], "current selected")
        self.assertEqual(deeper["content"]["qualifications"], ["finite domain only"])
        self.assertEqual(
            deeper["context_qualifications"]["restrictions"],
            ["exact normalization required"],
        )
        self.assertNotIn("UNRELATED NEW BODY", json.dumps(science))
        changes = science["source_changes"]
        self.assertEqual(len(changes), 3)
        first = next(change for change in changes
                     if change["cited_reference"] == self._context_ref(source1)
                     and change["selection"] == selected)
        second = next(change for change in changes
                      if change["cited_reference"] == self._context_ref(source2))
        other = next(change for change in changes if change["selection"] == sibling)
        first_index = changes.index(first)
        second_index = changes.index(second)
        other_index = changes.index(other)
        self.assertEqual(first["cited_reference"], self._context_ref(source1))
        self.assertEqual(second["cited_reference"], self._context_ref(source2))
        self.assertEqual(first["current_reference"], self._context_ref(source3))
        self.assertEqual(set(first["qualification"]["treatments"]), {"selected"})
        self.assertEqual(
            first["qualification"]["context_qualifications"]["known_omissions"],
            ["no growing-domain result"],
        )
        self.assertIsNone(first["qualification_ref"])
        self.assertIsNone(second["qualification"])
        self.assertEqual(second["qualification_ref"], {
            "orientation_path": f"/scientific_context/source_changes/{first_index}/qualification",
        })
        self.assertEqual(other["selection"], sibling)
        self.assertIsNone(other["qualification_ref"])
        self.assertEqual(set(other["qualification"]["treatments"]), {"sibling"})

        orientation = deep_thaw(self.interface.executive_orientation())
        orientation["mission"]["scientific_context_id"] = "scientific.root"
        orientation["scientific_context"] = science
        validate_executive_orientation(orientation)
        invalid = deep_thaw(orientation)
        treatment = invalid["scientific_context"]["treatments"][0]
        treatment.pop("content")
        treatment["content_ref"] = {
            "orientation_path": "/scientific_context/treatments/0/content",
        }
        with self.assertRaises(ValueError):
            validate_executive_orientation(invalid)
        invalid = deep_thaw(orientation)
        invalid["scientific_context"]["source_changes"][second_index]["qualification_ref"] = {
            "orientation_path": "/strategy_ground/0/summary",
        }
        with self.assertRaises(ValueError):
            validate_executive_orientation(invalid)
        invalid = deep_thaw(orientation)
        invalid["scientific_context"]["source_changes"][second_index]["qualification_ref"] = {
            "orientation_path": f"/scientific_context/source_changes/{other_index}/qualification",
        }
        with self.assertRaises(ValueError):
            validate_executive_orientation(invalid)


if __name__ == "__main__":
    unittest.main()
