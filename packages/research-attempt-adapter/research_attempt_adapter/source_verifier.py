"""Exact source binding before any provider launch effect."""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol, runtime_checkable

from .errors import SourceVerificationError
from .models import (
    AttemptIntent,
    require_commit,
    require_digest,
    require_safe_code,
    sha256_bytes,
)


@dataclass(frozen=True)
class SourceVerification:
    verifier_id: str
    source_commit: str
    source_digest: str

    def __post_init__(self) -> None:
        require_safe_code(self.verifier_id, "verifier_id")
        require_commit(self.source_commit)
        require_digest(self.source_digest, "source_digest")


@runtime_checkable
class SourceVerifier(Protocol):
    def verify(self, intent: AttemptIntent) -> SourceVerification:
        ...


class GitSourceVerifier:
    """Verify a clean Git worktree and digest its exact committed tree listing.

    `source_digest` is SHA-256 over the raw output of
    `git ls-tree -r -z --full-tree HEAD`. A caller using another canonical tree
    digest must inject a verifier for that exact algorithm instead of treating
    unlike digests as equivalent.
    """

    verifier_id = "git_ls_tree_sha256_v1"

    def verify(self, intent: AttemptIntent) -> SourceVerification:
        root = Path(intent.working_directory)
        head = self._git(root, "rev-parse", "--verify", "HEAD^{commit}").decode(
            "ascii"
        ).strip()
        if head != intent.source_commit:
            raise SourceVerificationError(
                "source_commit_mismatch",
                "The launch worktree HEAD does not match the frozen source commit.",
            )
        status = self._git(
            root,
            "status",
            "--porcelain=v1",
            "--untracked-files=all",
        )
        if status:
            raise SourceVerificationError(
                "source_worktree_dirty",
                "The launch worktree is not an exact clean source checkout.",
            )
        tree_listing = self._git(
            root,
            "ls-tree",
            "-r",
            "-z",
            "--full-tree",
            "HEAD",
        )
        digest = sha256_bytes(tree_listing)
        if digest != intent.source_digest:
            raise SourceVerificationError(
                "source_digest_mismatch",
                "The launch worktree tree digest does not match the frozen source digest.",
            )
        return SourceVerification(
            verifier_id=self.verifier_id,
            source_commit=head,
            source_digest=digest,
        )

    @staticmethod
    def _git(root: Path, *args: str) -> bytes:
        outcome = subprocess.run(
            ["git", "-C", str(root), *args],
            capture_output=True,
            check=False,
            shell=False,
        )
        if outcome.returncode != 0:
            raise SourceVerificationError(
                "source_git_verification_failed",
                "Git could not verify the exact launch worktree.",
            )
        return outcome.stdout
