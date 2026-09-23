"""Closed example selection and real owner genesis; no model/provider execution."""

from __future__ import annotations

import io
import json
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from contextlib import redirect_stderr, redirect_stdout


ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))
from scripts import research, rh_mission


EXAMPLE = "finite-free-localization"
MISSION_ID = "mission.rh.example.finite_free_localization.1"
COMMIT = "a" * 40


class BundledExampleTests(unittest.TestCase):
    def test_default_and_example_are_valid_distinct_seeds_with_same_policy(self) -> None:
        default = json.loads((ROOT / research.mission_seed_relative_path()).read_text())
        example = json.loads((ROOT / research.mission_seed_relative_path(EXAMPLE)).read_text())
        self.assertEqual(default["mission"]["mission_id"], "mission.rh.public.1")
        self.assertEqual(example["mission"]["mission_id"], MISSION_ID)
        for seed in (default, example):
            rh_mission.WorkspaceStore.validate_direct_mission_genesis_seed(
                seed, project_id=research.PROJECT, mission_id=seed["mission"]["mission_id"])
        self.assertEqual(default["mission"]["purpose"], example["mission"]["purpose"])
        self.assertEqual(default["mission"]["execution_policy"], example["mission"]["execution_policy"])
        self.assertEqual(example["strategy"]["mission_continuation"], "continue")

    def test_init_binds_selector_to_request_and_owner_command(self) -> None:
        for selected in (None, EXAMPLE):
            with self.subTest(example=selected), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                state = root / "state"
                args = SimpleNamespace(state_root=str(state), codex_home=str(root / "codex-home"),
                                       python=sys.executable, node=sys.executable, codex=sys.executable,
                                       example=selected)
                with (patch.object(research, "require_runtime_user"),
                      patch.object(research, "installed_release", return_value={"release_sha": COMMIT}) as installed,
                      patch.object(research, "owned_directory"),
                      patch.object(research.os, "getuid", return_value=1234, create=True),
                      patch.object(research, "environment", return_value={}),
                      patch.object(research, "run", return_value="{}") as run,
                      redirect_stdout(io.StringIO())):
                    research.initialize(args)
                installed.assert_called_once_with(example=selected)
                request = json.loads((state / "genesis-request.json").read_text())
                config = json.loads((state / research.CONFIG).read_text())
                expected_mission = MISSION_ID if selected else "mission.rh.public.1"
                self.assertEqual(request["mission_id"], expected_mission)
                self.assertEqual(config["mission_id"], expected_mission)
                self.assertEqual(request["source_commit"], COMMIT)
                argv = run.call_args.args[0]
                if selected:
                    self.assertEqual(request["example"], selected)
                    self.assertEqual(config["example"], selected)
                    self.assertEqual(argv[-2:], ["--example", selected])
                else:
                    self.assertNotIn("example", request)
                    self.assertNotIn("example", config)
                    self.assertNotIn("--example", argv)

    def test_example_owner_genesis_keeps_real_canonical_binding_and_no_results(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            workspace = root / "workspace"
            request = self._request(root, workspace)
            with (patch.object(rh_mission, "_read_owner_release_commit", return_value=COMMIT),
                  patch.object(research, "installed_release", return_value={"release_sha": COMMIT}) as installed):
                result = rh_mission._run_owner_genesis(
                    SimpleNamespace(request=str(request), example=EXAMPLE), self._options(workspace))
            installed.assert_called_once_with(example=EXAMPLE)
            self.assertEqual(result["example"], EXAMPLE)
            interface = rh_mission.MissionInterface.open(workspace, research.PROJECT, MISSION_ID)
            self.assertEqual(interface.current_epoch()["state"], "not_opened")
            with interface._store.snapshot_connection() as connection:
                stored_mission = json.loads(connection.execute(
                    "SELECT payload_json FROM mission_revision WHERE object_id = ?", (MISSION_ID,)
                ).fetchone()[0])
                authority = json.loads(connection.execute(
                    "SELECT canonical_authority_json FROM workspace_metadata WHERE singleton = 1"
                ).fetchone()[0])
                self.assertEqual(connection.execute("SELECT COUNT(*) FROM executive_epoch_event").fetchone()[0], 0)
            self.assertEqual(stored_mission["mission_id"], MISSION_ID)
            self.assertEqual(authority["source_commit"], COMMIT)
            self.assertEqual(authority["canonical_state_path"], "projects/riemann_hypothesis/research_state.json")

    def test_owner_rejects_selector_and_source_drift_before_creating_workspace(self) -> None:
        for change, message in (({"example": "other"}, "example selector"),
                                ({"source_commit": "b" * 40}, "installed release")):
            with self.subTest(change=change), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                workspace = root / "workspace"
                request = self._request(root, workspace, **change)
                with (patch.object(rh_mission, "_read_owner_release_commit", return_value=COMMIT),
                      patch.object(research, "installed_release") as installed,
                      self.assertRaisesRegex(ValueError, message)):
                    rh_mission._run_owner_genesis(
                        SimpleNamespace(request=str(request), example=EXAMPLE), self._options(workspace))
                installed.assert_not_called()
                self.assertFalse(workspace.exists())

    def test_unknown_selector_is_rejected_before_runtime_or_state_creation(self) -> None:
        with self.assertRaisesRegex(ValueError, "unknown bundled example"):
            research.mission_seed_relative_path("../outside")
        args = ["research.py", "init", "--example", "../outside"]
        for flag in ("state-root", "codex-home", "python", "node", "codex"):
            args.extend(["--" + flag, str(ROOT / "not-created")])
        with (patch.object(sys, "argv", args), patch.object(research, "initialize") as initialize,
              redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as stopped):
            research.main()
        self.assertEqual(stopped.exception.code, 2)
        initialize.assert_not_called()
        with self.assertRaisesRegex(ValueError, "invalid choice"):
            rh_mission._parser().parse_args(["owner-genesis", "--request", "unused", "--example", "../outside"])

    def test_example_genesis_requires_immutable_bundled_source(self) -> None:
        seed = ROOT / research.mission_seed_relative_path(EXAMPLE)
        def ordinary(path: Path, *, directory: bool = False) -> SimpleNamespace:
            return SimpleNamespace(st_uid=0, st_mode=0o666 if path == seed else 0o755)
        with (patch.object(research, "ordinary", side_effect=ordinary),
              self.assertRaisesRegex(ValueError, "not group/world writable")):
            research.installed_release(example=EXAMPLE)

    @staticmethod
    def _options(workspace: Path) -> dict[str, str]:
        return {"workspace_root": str(workspace), "project_id": research.PROJECT, "mission_id": MISSION_ID}

    @staticmethod
    def _request(root: Path, workspace: Path, **changes: str) -> Path:
        request = root / "request.json"
        request.write_text(json.dumps({
            "schema_version": rh_mission.MISSION_OWNER_GENESIS_REQUEST_SCHEMA_VERSION,
            "mission_id": MISSION_ID, "workspace_root": str(workspace), "source_commit": COMMIT,
            "example": EXAMPLE, **changes,
        }))
        return request


if __name__ == "__main__":
    unittest.main()
