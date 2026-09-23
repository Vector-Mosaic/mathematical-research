"""Exclusive mechanical writer for one admitted complete RH result.

Admission Review and Decision records are proof-neutral.  This module is the
separate canonical writer boundary: it accepts only one exact, persisted
``authorize_exact_delta`` Decision and derives the sole permitted Research
State change from it.  It never decides mathematics, changes Mission or
Strategy state, or publishes the result.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Mapping

from .canonical_snapshot import (
    CANONICAL_STATE_REPO_PATH,
    load_canonical_snapshot,
)
from .complete_claim_admission import (
    ADMISSION_DECISION_AUTHORIZE,
    AdmissionDecisionRecord,
)
from .json_support import loads_strict_json_bytes
from .research_model import deep_thaw
from .validator import validate_state


_FULL_GIT_SHA = re.compile(r"[0-9a-f]{40}")
_SHA256 = re.compile(r"[0-9a-f]{64}")
_PROJECT_ID = "project.riemann_hypothesis"


class CanonicalAdmissionError(ValueError):
    """Reject a stale, unauthorized, or non-exact canonical result write."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True, slots=True)
class CanonicalAdmissionWriteResult:
    state_path: str
    source_commit: str
    decision_id: str
    decision_digest: str
    disposition: str
    prestate_sha256: str
    poststate_sha256: str
    replayed: bool

    def to_payload(self) -> dict[str, object]:
        return {
            "state_path": self.state_path,
            "source_commit": self.source_commit,
            "decision_id": self.decision_id,
            "decision_digest": self.decision_digest,
            "disposition": self.disposition,
            "prestate_sha256": self.prestate_sha256,
            "poststate_sha256": self.poststate_sha256,
            "replayed": self.replayed,
            "canonical_effect": "exact_authorized_delta",
            "mission_effect": "none",
            "strategy_effect": "none",
            "public_effect": "none",
        }


def _sha256(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _read_git_state(repo_root: Path, source_commit: str) -> bytes:
    completed = subprocess.run(
        [
            "git",
            "-C",
            str(repo_root),
            "show",
            f"{source_commit}:{CANONICAL_STATE_REPO_PATH}",
        ],
        check=False,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    if completed.returncode != 0:
        raise CanonicalAdmissionError(
            "canonical_source_commit_unreadable",
            "the selected source commit does not expose the canonical Research State",
        )
    return completed.stdout


def _git_output(repo_root: Path, *arguments: str, code: str, message: str) -> bytes:
    completed = subprocess.run(
        ["git", "-C", str(repo_root), *arguments],
        check=False,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    if completed.returncode != 0:
        raise CanonicalAdmissionError(code, message)
    return completed.stdout


def _verify_clean_selected_worktree(repo_root: Path, source_commit: str) -> None:
    """Require the exclusive writer's exact clean source checkout."""

    head = _git_output(
        repo_root,
        "rev-parse",
        "--verify",
        "HEAD",
        code="canonical_worktree_invalid",
        message="the canonical writer root is not one readable Git worktree",
    ).decode("ascii", errors="strict").strip()
    if head != source_commit:
        raise CanonicalAdmissionError(
            "canonical_worktree_head_mismatch",
            "the canonical writer worktree HEAD differs from the selected source commit",
        )
    status = _git_output(
        repo_root,
        "status",
        "--porcelain=v1",
        "--untracked-files=all",
        code="canonical_worktree_invalid",
        message="the canonical writer could not verify worktree cleanliness",
    )
    if status:
        raise CanonicalAdmissionError(
            "canonical_worktree_dirty",
            "the canonical writer requires a completely clean selected worktree",
        )


def admitted_result_from_decision(
    decision: AdmissionDecisionRecord,
) -> dict[str, object]:
    """Derive the closed project-level result from one exact Decision."""

    if type(decision) is not AdmissionDecisionRecord:
        raise CanonicalAdmissionError(
            "admission_decision_invalid",
            "canonical writing requires an exact AdmissionDecisionRecord",
        )
    try:
        decision.verify_prepared()
    except (TypeError, ValueError) as exc:
        raise CanonicalAdmissionError(
            "admission_decision_invalid",
            "the Admission Decision failed exact reconstruction",
        ) from exc
    if (
        decision.disposition != ADMISSION_DECISION_AUTHORIZE
        or not isinstance(decision.canonical_result_binding, Mapping)
    ):
        raise CanonicalAdmissionError(
            "admission_decision_not_authorizing",
            "only authorize_exact_delta may change canonical Research State",
        )
    binding = deep_thaw(decision.canonical_result_binding)
    result = binding.get("result")
    if result not in {"proved", "disproved"}:
        raise CanonicalAdmissionError(
            "admission_decision_invalid",
            "the authorized Decision has no symmetric canonical disposition",
        )
    candidate_ref = decision.candidate_ref.to_payload()
    if binding.get("candidate_ref") != candidate_ref:
        raise CanonicalAdmissionError(
            "admission_decision_invalid",
            "the authorized Decision differs from its exact Candidate",
        )
    return {
        "target": "riemann_hypothesis",
        "disposition": result,
        "theorem_or_counterexample_claim": binding["exact_claim"],
        "candidate_ref": candidate_ref,
        "admission_decision_ref": {
            "decision_id": decision.evidence.evidence_id,
            "digest_sha256": decision.payload_digest,
        },
    }


def prepare_admitted_research_state(
    state: Mapping[str, object],
    decision: AdmissionDecisionRecord,
) -> tuple[dict[str, object], dict[str, object], bool]:
    """Return the exact terminal state, result, and replay disposition."""

    if not isinstance(state, Mapping):
        raise CanonicalAdmissionError(
            "canonical_prestate_invalid", "canonical Research State must be an object"
        )
    prepared = deep_thaw(state)
    project = prepared.get("project")
    if not isinstance(project, dict) or project.get("id") != _PROJECT_ID:
        raise CanonicalAdmissionError(
            "canonical_prestate_invalid",
            "canonical Research State does not own the RH project",
        )
    admitted_result = admitted_result_from_decision(decision)
    existing = project.get("admitted_result")
    if existing is not None or project.get("proof_status") in {"proved", "disproved"}:
        if (
            existing == admitted_result
            and project.get("proof_status") == admitted_result["disposition"]
            and "active_target" not in project
        ):
            validation = validate_state(prepared)
            if validation.ok:
                return prepared, admitted_result, True
        raise CanonicalAdmissionError(
            "canonical_result_conflict",
            "canonical Research State already carries another terminal disposition",
        )
    if project.get("proof_status") != "incomplete" or not isinstance(
        project.get("active_target"), str
    ):
        raise CanonicalAdmissionError(
            "canonical_prestate_invalid",
            "canonical RH state is not the exact incomplete active-target prestate",
        )
    project["proof_status"] = admitted_result["disposition"]
    project["admitted_result"] = admitted_result
    del project["active_target"]
    validation = validate_state(prepared)
    if not validation.ok:
        raise CanonicalAdmissionError(
            "canonical_poststate_invalid",
            "the exact admitted-result delta does not validate as Research State",
        )
    return prepared, admitted_result, False


def _render_minimal_delta(
    raw: bytes,
    *,
    original_state: Mapping[str, object],
    prepared_state: Mapping[str, object],
    admitted_result: Mapping[str, object],
) -> bytes:
    """Preserve the large historical file byte-for-byte outside project truth."""

    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise CanonicalAdmissionError(
            "canonical_prestate_invalid", "canonical Research State is not UTF-8"
        ) from exc
    project = original_state.get("project")
    if not isinstance(project, Mapping):
        raise CanonicalAdmissionError(
            "canonical_prestate_invalid", "canonical project object is absent"
        )
    proof_line = '    "proof_status": "incomplete",\n'
    target_line = (
        "    \"active_target\": "
        + json.dumps(project.get("active_target"), ensure_ascii=False)
        + ",\n"
    )
    if text.count(proof_line) != 1 or text.count(target_line) != 1:
        raise CanonicalAdmissionError(
            "canonical_prestate_shape_changed",
            "canonical project formatting differs from the exact minimal-delta writer",
        )
    encoded_result = json.dumps(
        deep_thaw(admitted_result), ensure_ascii=False, indent=2
    ).splitlines()
    admitted_lines = [
        '    "admitted_result": ' + encoded_result[0],
        *("    " + line for line in encoded_result[1:]),
    ]
    replacement = (
        f'    "proof_status": "{admitted_result["disposition"]}",\n'
        + "\n".join(admitted_lines)
        + ",\n"
    )
    rendered = text.replace(proof_line, replacement, 1).replace(target_line, "", 1)
    rendered_raw = rendered.encode("utf-8")
    try:
        reparsed = loads_strict_json_bytes(rendered_raw)
    except (TypeError, ValueError, UnicodeError) as exc:
        raise CanonicalAdmissionError(
            "canonical_poststate_invalid", "rendered Research State is not strict JSON"
        ) from exc
    if reparsed != prepared_state:
        raise CanonicalAdmissionError(
            "canonical_poststate_invalid",
            "minimal rendering changed more or less than the exact admitted-result delta",
        )
    return rendered_raw


def _prepare_admitted_result_write(
    repo_root: str | Path,
    *,
    source_commit: str,
    expected_prestate_sha256: str,
    decision: AdmissionDecisionRecord,
    _commit_reader: Callable[[Path, str], bytes] = _read_git_state,
    _worktree_verifier: Callable[[Path, str], None] = (
        _verify_clean_selected_worktree
    ),
) -> tuple[CanonicalAdmissionWriteResult, Path, bytes]:
    """Validate and render the exact Decision-derived canonical delta."""

    root = Path(repo_root).resolve()
    if _FULL_GIT_SHA.fullmatch(source_commit) is None:
        raise CanonicalAdmissionError(
            "canonical_source_commit_invalid", "source_commit must be one full Git SHA"
        )
    if _SHA256.fullmatch(expected_prestate_sha256) is None:
        raise CanonicalAdmissionError(
            "canonical_prestate_digest_invalid",
            "expected_prestate_sha256 must be lowercase SHA-256",
        )
    _worktree_verifier(root, source_commit)
    loaded = load_canonical_snapshot(root, source_commit=source_commit)
    if not loaded.ok or loaded.value is None:
        message = loaded.failure.message if loaded.failure is not None else "unknown"
        raise CanonicalAdmissionError(
            "canonical_prestate_invalid", f"canonical Research State is invalid: {message}"
        )
    snapshot = loaded.value
    state_path = snapshot.authorized_path
    raw = state_path.read_bytes()
    prestate_sha256 = _sha256(raw)
    if prestate_sha256 != expected_prestate_sha256:
        raise CanonicalAdmissionError(
            "canonical_prestate_stale",
            "canonical Research State differs from the authorized prestate digest",
        )
    if _commit_reader(root, source_commit) != raw:
        raise CanonicalAdmissionError(
            "canonical_prestate_uncommitted",
            "canonical Research State differs from the selected source commit",
        )
    prepared, admitted_result, replayed = prepare_admitted_research_state(
        snapshot.state, decision
    )
    if replayed:
        post_raw = raw
    else:
        post_raw = _render_minimal_delta(
            raw,
            original_state=snapshot.state,
            prepared_state=prepared,
            admitted_result=admitted_result,
        )
    result = CanonicalAdmissionWriteResult(
        state_path=CANONICAL_STATE_REPO_PATH,
        source_commit=source_commit,
        decision_id=decision.evidence.evidence_id,
        decision_digest=decision.payload_digest,
        disposition=str(admitted_result["disposition"]),
        prestate_sha256=prestate_sha256,
        poststate_sha256=_sha256(post_raw),
        replayed=replayed,
    )
    return result, state_path, post_raw


def preview_admitted_result(
    repo_root: str | Path,
    *,
    source_commit: str,
    expected_prestate_sha256: str,
    decision: AdmissionDecisionRecord,
    _commit_reader: Callable[[Path, str], bytes] = _read_git_state,
    _worktree_verifier: Callable[[Path, str], None] = (
        _verify_clean_selected_worktree
    ),
) -> CanonicalAdmissionWriteResult:
    """Validate and compute the exact canonical result without writing it."""

    result, _state_path, _post_raw = _prepare_admitted_result_write(
        repo_root,
        source_commit=source_commit,
        expected_prestate_sha256=expected_prestate_sha256,
        decision=decision,
        _commit_reader=_commit_reader,
        _worktree_verifier=_worktree_verifier,
    )
    return result


def write_admitted_result(
    repo_root: str | Path,
    *,
    source_commit: str,
    expected_prestate_sha256: str,
    decision: AdmissionDecisionRecord,
    _commit_reader: Callable[[Path, str], bytes] = _read_git_state,
    _worktree_verifier: Callable[[Path, str], None] = (
        _verify_clean_selected_worktree
    ),
) -> CanonicalAdmissionWriteResult:
    """Atomically apply one exact Decision-derived canonical delta.

    Git commit/push remains the controller's ordinary clean-worktree operation;
    this function changes only the canonical Research State file.
    """

    result, state_path, post_raw = _prepare_admitted_result_write(
        repo_root,
        source_commit=source_commit,
        expected_prestate_sha256=expected_prestate_sha256,
        decision=decision,
        _commit_reader=_commit_reader,
        _worktree_verifier=_worktree_verifier,
    )
    if not result.replayed:
        mode = stat.S_IMODE(state_path.stat().st_mode)
        temporary_path: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="wb",
                prefix=f".{state_path.name}.admission-",
                dir=state_path.parent,
                delete=False,
            ) as handle:
                handle.write(post_raw)
                handle.flush()
                os.fsync(handle.fileno())
                temporary_path = Path(handle.name)
            os.chmod(temporary_path, mode)
            os.replace(temporary_path, state_path)
            temporary_path = None
        finally:
            if temporary_path is not None:
                temporary_path.unlink(missing_ok=True)
    return result


__all__ = [
    "CanonicalAdmissionError",
    "CanonicalAdmissionWriteResult",
    "admitted_result_from_decision",
    "prepare_admitted_research_state",
    "preview_admitted_result",
    "write_admitted_result",
]
