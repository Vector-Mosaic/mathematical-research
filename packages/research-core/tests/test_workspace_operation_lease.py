from __future__ import annotations

import json
import hashlib
import os
import sys
import tempfile
import unittest
from pathlib import Path


PACKAGE_ROOT = Path(__file__).resolve().parents[1]
if str(PACKAGE_ROOT) not in sys.path:
    sys.path.insert(0, str(PACKAGE_ROOT))

from research_core.workspace_operation_lease import (  # noqa: E402
    KernelLeaseError,
    acquire_kernel_lease,
)
from research_core.migration_executor import (  # noqa: E402
    MigrationExecutionError,
    _acquire_migration_lease,
)
from research_core.workspace_paths import WorkspacePaths  # noqa: E402


class WorkspaceOperationLeaseTests(unittest.TestCase):
    def test_live_descriptor_is_proof_and_metadata_closes_on_release(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            lock_path = Path(directory).resolve() / "operation.lock"
            with acquire_kernel_lease(
                lock_path,
                format_name="test-kernel-lease-v1",
                scope={"project_id": "riemann_hypothesis", "actor": "test"},
            ) as lease:
                lease.assert_held()
                self.assertEqual(lease.path, lock_path)
                self.assertFalse(lease.prior_abandoned)
                self.assertEqual(len(lease.scope_sha256), 64)
                with self.assertRaises(AttributeError):
                    lease.path = lock_path.parent / "forged.lock"  # type: ignore[misc]

            released = json.loads(lock_path.read_text(encoding="utf-8"))
            self.assertEqual(released["lifecycle"], "released")
            self.assertIsNotNone(released["released_at"])
            self.assertEqual(released["lease_id"], lease.lease_id)
            self.assertEqual(released["project_id"], "riemann_hypothesis")
            with self.assertRaises(KernelLeaseError) as stale:
                lease.assert_held()
            self.assertEqual(stale.exception.code, "kernel_lease_not_held")

    def test_second_executor_cannot_acquire_same_kernel_lock(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            lock_path = Path(directory).resolve() / "operation.lock"
            with acquire_kernel_lease(
                lock_path,
                format_name="test-kernel-lease-v1",
                scope={"project_id": "riemann_hypothesis"},
            ):
                with self.assertRaises(KernelLeaseError) as busy:
                    with acquire_kernel_lease(
                        lock_path,
                        format_name="test-kernel-lease-v1",
                        scope={"project_id": "riemann_hypothesis"},
                    ):
                        self.fail("contended kernel lease was acquired")
                self.assertEqual(busy.exception.code, "kernel_lease_busy")

    def test_held_predecessor_metadata_is_diagnostic_not_authority(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            lock_path = Path(directory).resolve() / "operation.lock"
            lock_path.write_text(
                json.dumps(
                    {
                        "format": "test-kernel-lease-v1",
                        "lease_id": "lease-dead-process",
                        "lifecycle": "held",
                    }
                ),
                encoding="utf-8",
            )
            with acquire_kernel_lease(
                lock_path,
                format_name="test-kernel-lease-v1",
                scope={"project_id": "riemann_hypothesis"},
            ) as lease:
                self.assertTrue(lease.prior_abandoned)
                self.assertIsNone(lease.predecessor_lease_id)
                self.assertIsNone(lease.predecessor_scope_sha256)
                lease.assert_held()
            current = json.loads(lock_path.read_text(encoding="utf-8"))
            self.assertIsNone(current["predecessor_lease_id"])

    def test_exact_abandoned_predecessor_exposes_identity_and_same_scope(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            lock_path = Path(directory).resolve() / "operation.lock"
            scope = {"project_id": "riemann_hypothesis", "actor": "test"}
            predecessor_lease_id = "lease-" + "a" * 32
            lock_path.write_text(
                json.dumps(
                    {
                        "format": "test-kernel-lease-v1",
                        **scope,
                        "lease_id": predecessor_lease_id,
                        "acquired_at": "2026-08-23T00:00:00.000000Z",
                        "released_at": None,
                        "lifecycle": "held",
                        "predecessor_lease_id": None,
                        "predecessor_abandoned": False,
                    }
                ),
                encoding="utf-8",
            )

            with acquire_kernel_lease(
                lock_path,
                format_name="test-kernel-lease-v1",
                scope=scope,
            ) as lease:
                self.assertTrue(lease.prior_abandoned)
                self.assertEqual(
                    lease.predecessor_lease_id,
                    predecessor_lease_id,
                )
                self.assertEqual(
                    lease.predecessor_scope_sha256,
                    hashlib.sha256(
                        json.dumps(
                            scope,
                            ensure_ascii=False,
                            allow_nan=False,
                            sort_keys=True,
                            separators=(",", ":"),
                        ).encode("utf-8")
                    ).hexdigest(),
                )
                self.assertEqual(
                    lease.predecessor_scope_sha256,
                    lease.scope_sha256,
                )

    def test_abandoned_predecessor_with_different_scope_is_not_adoptable(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            lock_path = Path(directory).resolve() / "operation.lock"
            predecessor_lease_id = "lease-" + "b" * 32
            lock_path.write_text(
                json.dumps(
                    {
                        "format": "test-kernel-lease-v1",
                        "project_id": "another_project",
                        "lease_id": predecessor_lease_id,
                        "acquired_at": "2026-08-23T00:00:00.000000Z",
                        "released_at": None,
                        "lifecycle": "held",
                        "predecessor_lease_id": None,
                        "predecessor_abandoned": False,
                    }
                ),
                encoding="utf-8",
            )

            with acquire_kernel_lease(
                lock_path,
                format_name="test-kernel-lease-v1",
                scope={"project_id": "riemann_hypothesis"},
            ) as lease:
                self.assertTrue(lease.prior_abandoned)
                self.assertIsNone(lease.predecessor_lease_id)
                self.assertIsNone(lease.predecessor_scope_sha256)

    def test_relative_missing_parent_and_symlink_paths_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory).resolve()
            with self.assertRaises(KernelLeaseError) as relative:
                with acquire_kernel_lease(
                    "operation.lock",
                    format_name="test-kernel-lease-v1",
                    scope={},
                ):
                    self.fail("relative lease path was accepted")
            self.assertEqual(relative.exception.code, "kernel_lease_unsafe")

            with self.assertRaises(KernelLeaseError) as missing_parent:
                with acquire_kernel_lease(
                    base / "missing" / "operation.lock",
                    format_name="test-kernel-lease-v1",
                    scope={},
                ):
                    self.fail("missing lease parent was accepted")
            self.assertEqual(missing_parent.exception.code, "kernel_lease_unsafe")

            target = base / "target.lock"
            target.write_text("", encoding="utf-8")
            link = base / "linked.lock"
            try:
                link.symlink_to(target)
            except OSError:
                return
            with self.assertRaises(KernelLeaseError) as unsafe:
                with acquire_kernel_lease(
                    link,
                    format_name="test-kernel-lease-v1",
                    scope={},
                ):
                    self.fail("symlink lease path was accepted")
            self.assertEqual(unsafe.exception.code, "kernel_lease_unsafe")

    def test_hardlinked_lock_is_rejected_before_metadata_write(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory).resolve()
            external = base / "external.txt"
            external.write_bytes(b"preserve-external-lock-target")
            lock_path = base / "operation.lock"
            os.link(external, lock_path)

            with self.assertRaises(KernelLeaseError) as unsafe:
                with acquire_kernel_lease(
                    lock_path,
                    format_name="test-kernel-lease-v1",
                    scope={"project_id": "riemann_hypothesis"},
                ):
                    self.fail("hardlinked kernel lease was accepted")

            self.assertEqual(unsafe.exception.code, "kernel_lease_unsafe")
            self.assertEqual(
                external.read_bytes(), b"preserve-external-lock-target"
            )
            self.assertEqual(lock_path.stat().st_nlink, 2)

    def test_migration_adapter_preserves_lock_artifact_metadata_and_busy_error(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            paths = WorkspacePaths.from_root(Path(directory).resolve() / "workspace")
            paths.recovery.mkdir(parents=True)
            with _acquire_migration_lease(
                paths,
                migration_id="adapter-parity",
                actor="test.adapter",
                fault_hook=None,
            ) as lease:
                lease.assert_held()
                self.assertEqual(
                    lease.path,
                    paths.recovery / ".migration-adapter-parity.executor.lock",
                )
                with self.assertRaises(MigrationExecutionError) as busy:
                    with _acquire_migration_lease(
                        paths,
                        migration_id="adapter-parity",
                        actor="test.second",
                        fault_hook=None,
                    ):
                        self.fail("migration adapter acquired a contended lease")
                self.assertEqual(busy.exception.code, "migration_executor_busy")
                self.assertEqual(
                    busy.exception.artifact,
                    paths.recovery / "migration-adapter-parity",
                )

            metadata = json.loads(lease.path.read_text(encoding="utf-8"))
            self.assertEqual(metadata["format"], "research-workspace-migration-lock-v1")
            self.assertEqual(metadata["migration_id"], "adapter-parity")
            self.assertEqual(metadata["actor"], "test.adapter")
            self.assertEqual(metadata["lifecycle"], "released")


if __name__ == "__main__":
    unittest.main()
