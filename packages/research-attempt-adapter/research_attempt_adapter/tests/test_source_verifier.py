from __future__ import annotations

import subprocess
import tempfile
import unittest
from pathlib import Path

from research_attempt_adapter import (
    AttemptIntent,
    GitSourceVerifier,
    NetworkPolicy,
    ResourceRequest,
    SourceVerificationError,
    sha256_bytes,
)


def _git(root: Path, *args: str) -> bytes:
    outcome = subprocess.run(
        ["git", "-C", str(root), *args],
        capture_output=True,
        check=True,
        shell=False,
    )
    return outcome.stdout


class GitSourceVerifierTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        _git(self.root, "init")
        _git(self.root, "config", "user.email", "test@example.invalid")
        _git(self.root, "config", "user.name", "Formal Test")
        (self.root / "source.txt").write_text("exact source\n", encoding="utf-8")
        _git(self.root, "add", "source.txt")
        _git(self.root, "commit", "-m", "source")
        self.commit = _git(self.root, "rev-parse", "HEAD").decode().strip()
        self.tree_digest = sha256_bytes(
            _git(self.root, "ls-tree", "-r", "-z", "--full-tree", "HEAD")
        )

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _intent(self) -> AttemptIntent:
        return AttemptIntent(
            attempt_id="attempt-source",
            session_id="session-source",
            session_digest="2" * 64,
            previous_attempt_id=None,
            correction_basis=None,
            readiness_state_digest="3" * 64,
            coordination_id="coord-source",
            coordination_digest="4" * 64,
            authorization_id="auth-source",
            authorization_digest="5" * 64,
            source_commit=self.commit,
            source_digest=self.tree_digest,
            provider_id="fake_provider",
            provider_profile="fake-provider.v2",
            model_profile="fake-model",
            reasoning_effort="ultra",
            resource_request=ResourceRequest(),
            baseline_capabilities=("shell",),
            network_policy=NetworkPolicy.DENIED,
            outer_containment_id=None,
            outer_containment_digest=None,
            source_attachments=(),
            working_directory=str(self.root),
        )

    def test_exact_clean_commit_and_tree_digest_are_verified(self) -> None:
        result = GitSourceVerifier().verify(self._intent())
        self.assertEqual(result.source_commit, self.commit)
        self.assertEqual(result.source_digest, self.tree_digest)

    def test_dirty_worktree_is_rejected(self) -> None:
        (self.root / "source.txt").write_text("changed\n", encoding="utf-8")
        with self.assertRaisesRegex(SourceVerificationError, "clean source checkout"):
            GitSourceVerifier().verify(self._intent())


if __name__ == "__main__":
    unittest.main()
