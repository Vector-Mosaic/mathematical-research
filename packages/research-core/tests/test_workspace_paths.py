from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


PACKAGE_ROOT = Path(__file__).resolve().parents[1]
if str(PACKAGE_ROOT) not in sys.path:
    sys.path.insert(0, str(PACKAGE_ROOT))

from research_core.workspace_paths import (  # noqa: E402
    WorkspacePathError,
    WorkspacePaths,
    _issue_principal_attestation,
    attest_current_principal,
    default_workspace_root,
    normalize_logical_path,
    stable_principal_owner_binding,
    verify_principal_attestation,
)


class WorkspacePathTests(unittest.TestCase):
    def test_defaults_are_platform_specific_and_never_create_directories(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            windows = default_workspace_root(
                platform_name="windows",
                environ={"LOCALAPPDATA": str(base / "local")},
            )
            linux = default_workspace_root(
                platform_name="linux",
                environ={"XDG_STATE_HOME": str(base / "state")},
            )
            self.assertEqual(
                windows,
                base
                / "local"
                / "VectorMosaic"
                / "mathematical_research"
                / "projects"
                / "riemann_hypothesis",
            )
            self.assertEqual(
                linux,
                base
                / "state"
                / "vector_mosaic"
                / "mathematical_research"
                / "projects"
                / "riemann_hypothesis",
            )
            self.assertFalse(windows.exists())
            self.assertFalse(linux.exists())

            configured = WorkspacePaths.default(
                platform_name="windows",
                environ={"LOCALAPPDATA": str(base / "configured")},
            )
            self.assertFalse(configured.root.exists())
            self.assertFalse(configured.database.exists())

    def test_root_rejects_relative_traversal_sync_network_artifact_and_git(self) -> None:
        with self.assertRaisesRegex(WorkspacePathError, "absolute"):
            WorkspacePaths.from_root("relative/workspace")
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory).resolve()
            with self.assertRaisesRegex(WorkspacePathError, "traversal"):
                WorkspacePaths.from_root(base / "allowed" / ".." / "escaped")
            with self.assertRaisesRegex(WorkspacePathError, "sync"):
                WorkspacePaths.from_root(base / "OneDrive" / "research")
            with self.assertRaisesRegex(WorkspacePathError, "artifacts"):
                WorkspacePaths.from_root(base / "artifacts" / "research")

            repository = base / "repository"
            repository.mkdir()
            (repository / ".git").write_text("gitdir: elsewhere", encoding="utf-8")
            with self.assertRaisesRegex(WorkspacePathError, "Git"):
                WorkspacePaths.from_root(repository / "nested" / "workspace")

        with self.assertRaisesRegex(WorkspacePathError, "local filesystem"):
            WorkspacePaths.from_root(r"\\server\share\research")
        with self.assertRaisesRegex(WorkspacePathError, "local filesystem"):
            WorkspacePaths.from_root("smb://server/share/research")

    def test_logical_paths_are_deterministic_and_contained(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            paths = WorkspacePaths.from_root(Path(directory) / "workspace")
            self.assertEqual(
                normalize_logical_path("cas/sha256/ab/value"),
                "cas/sha256/ab/value",
            )
            resolved = paths.resolve_logical("staging/ingestion-1/payload.json")
            self.assertEqual(
                paths.logical_from_path(resolved),
                "staging/ingestion-1/payload.json",
            )
            self.assertFalse(paths.root.exists())
            with self.assertRaises(WorkspacePathError):
                paths.resolve_logical("../outside")
            with self.assertRaises(WorkspacePathError):
                paths.resolve_logical(".")
            with self.assertRaises(WorkspacePathError):
                paths.resolve_logical("/absolute")
            with self.assertRaises(WorkspacePathError):
                paths.resolve_logical(r"staging\ambiguous")
            with self.assertRaises(WorkspacePathError):
                paths.logical_from_path(Path(directory).parent / "outside")

    def test_cas_layout_uses_lowercase_digest_fanout(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            paths = WorkspacePaths.from_root(Path(directory) / "workspace")
            digest = "AB" + "c" * 62
            expected = paths.root / "cas" / "sha256" / "ab" / ("c" * 62)
            self.assertEqual(paths.cas_path(digest), expected)
            with self.assertRaisesRegex(WorkspacePathError, "64"):
                paths.cas_path("abc")

    def test_root_identity_is_stable_but_does_not_expose_path(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            first = WorkspacePaths.from_root(Path(directory) / "workspace")
            second = WorkspacePaths.from_root(Path(directory) / "workspace")
            self.assertEqual(first.root_identity, second.root_identity)
            self.assertEqual(len(first.root_identity), 64)
            self.assertNotIn(str(first.root), first.root_identity)

    def test_symlink_roots_and_post_validation_reparse_points_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            target = base / "physical"
            target.mkdir()
            link = base / "linked"
            try:
                link.symlink_to(target, target_is_directory=True)
            except OSError:
                pass
            else:
                with self.assertRaisesRegex(WorkspacePathError, "symlink|reparse"):
                    WorkspacePaths.from_root(link)

            root = base / "workspace"
            root.mkdir()
            paths = WorkspacePaths.from_root(root)
            paths.staging.mkdir()
            with patch(
                "research_core.workspace_paths._is_reparse_point",
                side_effect=lambda value: Path(value) == paths.staging,
            ):
                with self.assertRaisesRegex(WorkspacePathError, "plain directory"):
                    paths.revalidate_physical(require_root=True)

    def test_physical_root_replacement_invalidates_frozen_binding(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "workspace"
            root.mkdir()
            paths = WorkspacePaths.from_root(root)
            displaced = Path(directory) / "displaced"
            root.rename(displaced)
            root.mkdir()
            with self.assertRaisesRegex(WorkspacePathError, "identity changed"):
                paths.revalidate_physical(require_root=True)

    def test_linux_principal_attestation_is_sealed_process_and_user_bound(self) -> None:
        def issue(*, uid: int, name: str, session_id: int):
            return _issue_principal_attestation(
                {
                    "host_name": "research-fixture-host",
                    "principal_name": name,
                    "principal_sid": f"linux-uid:{uid}:effective-uid:{uid}",
                    "session_id": session_id,
                    "process_id": os.getpid(),
                },
                platform_family="linux",
            )

        principal = issue(uid=1000, name="rh-runtime", session_id=7)
        same_principal = issue(uid=1000, name="renamed-runtime", session_id=99)
        foreign_principal = issue(uid=2000, name="other-runtime", session_id=7)
        verify_principal_attestation(principal)
        self.assertEqual(principal.platform_family, "linux")
        self.assertEqual(
            stable_principal_owner_binding(principal),
            stable_principal_owner_binding(same_principal),
        )
        self.assertNotEqual(
            stable_principal_owner_binding(principal),
            stable_principal_owner_binding(foreign_principal),
        )

        object.__setattr__(principal, "principal_sid", "linux-uid:0:effective-uid:0")
        with self.assertRaisesRegex(WorkspacePathError, "seal"):
            verify_principal_attestation(principal)

        with self.assertRaisesRegex(WorkspacePathError, "this process"):
            _issue_principal_attestation(
                {
                    "host_name": "research-fixture-host",
                    "principal_name": "rh-runtime",
                    "principal_sid": "linux-uid:1000:effective-uid:1000",
                    "session_id": 7,
                    "process_id": os.getpid() + 1,
                },
                platform_family="linux",
            )

    def test_current_principal_dispatches_to_linux_os_material(self) -> None:
        material = {
            "host_name": "research-fixture-host",
            "principal_name": "rh-runtime",
            "principal_sid": "linux-uid:1000:effective-uid:1000",
            "session_id": 7,
            "process_id": os.getpid(),
        }
        with (
            patch(
                "research_core.workspace_paths._platform_family",
                return_value="linux",
            ),
            patch("research_core.workspace_paths.os.name", "posix"),
            patch(
                "research_core.workspace_paths._linux_principal_material",
                return_value=material,
            ),
        ):
            attestation = attest_current_principal()
        verify_principal_attestation(attestation)
        self.assertEqual(attestation.platform_family, "linux")
        self.assertEqual(attestation.principal_sid, material["principal_sid"])


if __name__ == "__main__":
    unittest.main()
