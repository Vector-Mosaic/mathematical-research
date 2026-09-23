"""Closed stopped-owner adapter; atomic Context semantics are covered in Core."""
from __future__ import annotations

import copy
import importlib.util
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

REPO_ROOT = Path(__file__).resolve().parents[3]
SPEC = importlib.util.spec_from_file_location("rh_mission_revision_tool", REPO_ROOT / "scripts/rh_mission.py")
assert SPEC is not None and SPEC.loader is not None
TOOL = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = TOOL
SPEC.loader.exec_module(TOOL)


class ScientificContextRevisionCliTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.options = {"workspace_root": str(self.root), "project_id": "project.riemann_hypothesis",
                        "mission_id": "mission.rh.public.1", "format": "json"}
        self.request = {
            "schema_version": "mathematical_research.mission_scientific_context_revision_request.v1",
            "project_id": "project.riemann_hypothesis", "mission_id": "mission.rh.public.1",
            "workspace_root": str(self.root), "target_release_sha": "a" * 40,
            "expected_mission_revision": 6, "expected_mission_payload_sha256": "b" * 64,
            "expected_canonical_authority_digest": "c" * 64,
            "expected_store_cut": {"project_commit": 93, "current_root_digest": "d" * 64,
                                   "transition_head_digest": "e" * 64, "canonical_authority_digest": "c" * 64},
            "update": {"context_id": "context:science.rh", "expected_head": {
                "kind": "context", "identity": "science.rh", "revision": 1, "payload_sha256": "f" * 64,
            }, "create": None, "patch": {"opaque_science_for_core": True}},
        }
        self.interface = Mock()
        self.interface.revise_scientific_context_from_owner.return_value = {"idempotent": False}

    def invoke(self, request=None):
        with patch.object(TOOL, "_read_request", return_value=self.request if request is None else request), \
             patch.object(TOOL, "_read_owner_release_commit", return_value="a" * 40), \
             patch.object(TOOL.MissionInterface, "open", return_value=self.interface) as opened, \
             patch.object(TOOL, "load_canonical_snapshot", side_effect=AssertionError("no canonical scan")):
            result = TOOL._run_owner_scientific_context_revision(SimpleNamespace(request="request.json"), self.options)
            opened.assert_called_once_with(self.root, expected_project_id="project.riemann_hypothesis",
                                           expected_mission_id="mission.rh.public.1")
            return result

    def test_exact_patch_reaches_stopped_owner_without_runtime_open_or_digest(self):
        self.assertEqual(self.invoke(), {"idempotent": False})
        self.interface.revise_scientific_context_from_owner.assert_called_once_with(**{
            key: self.request[key] for key in ("expected_mission_revision", "expected_mission_payload_sha256",
                "expected_canonical_authority_digest", "expected_store_cut", "update")
        })

    def test_wrong_release_or_scope_and_creation_are_rejected_before_owner(self):
        for key, value in (("target_release_sha", "0" * 40), ("mission_id", "mission.other"),
                           ("expected_mission_revision", True), ("unexpected", True)):
            with self.subTest(key=key):
                request = copy.deepcopy(self.request)
                request[key] = value
                with self.assertRaises(ValueError):
                    self.invoke(request)
        request = copy.deepcopy(self.request)
        request["update"]["create"] = {"not": "a revision"}
        with self.assertRaises(ValueError):
            self.invoke(request)
        self.interface.revise_scientific_context_from_owner.assert_not_called()

    def test_lost_reply_does_not_automatically_retry_owner(self):
        self.interface.revise_scientific_context_from_owner.side_effect = RuntimeError("lost reply")
        with self.assertRaisesRegex(RuntimeError, "lost reply"):
            self.invoke()
        self.interface.revise_scientific_context_from_owner.assert_called_once()


if __name__ == "__main__":
    unittest.main()
