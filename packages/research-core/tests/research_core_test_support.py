from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import subprocess
import sys
from collections.abc import Iterator, Mapping
from pathlib import Path
from typing import Any

from research_core.json_support import loads_strict_json_bytes


_SOURCE_COMMIT_RE = re.compile(r"[0-9a-f]{40}")

ROOT6_BODY_KEYS = frozenset(
    {
        "root_digest_version",
        "project_id",
        "operating_mode",
        "schema_contract",
        "project_commit_no",
        "canonical_authority_digest",
        "predecessor_project_commit_no",
        "predecessor_root_digest",
        "predecessor_transition_head_digest",
        "transition_head_digest",
    }
)
ROOT6_SCHEMA_CONTRACT_KEYS = frozenset(
    {"schema_version", "migration_set_digest", "schema_object_digest"}
)
_ROOT6_DOMAIN = b"mathematical_research.workspace_root.v6\x00"


def _different_digest(value: object, marker: str) -> str:
    candidate = marker * 64
    return ("0" * 64) if value == candidate else candidate


def independent_root6_digest(body: Mapping[str, Any]) -> str:
    """Recompute the published root6 formula without a production helper."""

    if set(body) != ROOT6_BODY_KEYS:
        raise AssertionError("root6 oracle body must contain exactly ten keys")
    schema_contract = body["schema_contract"]
    if (
        not isinstance(schema_contract, Mapping)
        or set(schema_contract) != ROOT6_SCHEMA_CONTRACT_KEYS
    ):
        raise AssertionError("root6 oracle schema contract is not exact")
    encoded = json.dumps(
        body,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(_ROOT6_DOMAIN + encoded).hexdigest()


def independent_root6_body_at_commit(
    connection: sqlite3.Connection,
    *,
    schema_contract: Mapping[str, Any],
    project_commit_no: int,
) -> tuple[dict[str, Any], str]:
    """Build the literal ten-key body from persisted commit rows only."""

    current = connection.execute(
        "SELECT commit_no, project_id, root_digest, "
        "canonical_authority_digest, transition_head_digest "
        "FROM project_commit WHERE commit_no = ?",
        (project_commit_no,),
    ).fetchone()
    if current is None:
        raise AssertionError("root6 oracle current commit is absent")
    predecessor = (
        connection.execute(
            "SELECT commit_no, project_id, root_digest, "
            "canonical_authority_digest, transition_head_digest "
            "FROM project_commit WHERE commit_no = ?",
            (project_commit_no - 1,),
        ).fetchone()
        if project_commit_no
        else None
    )
    if project_commit_no and predecessor is None:
        raise AssertionError("root6 oracle predecessor commit is absent")
    if predecessor is not None and str(predecessor[1]) != str(current[1]):
        raise AssertionError("root6 oracle predecessor belongs to another project")
    body = {
        "root_digest_version": 6,
        "project_id": str(current[1]),
        "operating_mode": "mission_runtime",
        "schema_contract": dict(schema_contract),
        "project_commit_no": int(current[0]),
        "canonical_authority_digest": str(current[3]),
        "predecessor_project_commit_no": (
            None if predecessor is None else int(predecessor[0])
        ),
        "predecessor_root_digest": (
            None if predecessor is None else str(predecessor[2])
        ),
        "predecessor_transition_head_digest": (
            None if predecessor is None or predecessor[4] is None else str(predecessor[4])
        ),
        "transition_head_digest": (
            None if current[4] is None else str(current[4])
        ),
    }
    return body, str(current[2])


def independent_root6_member_mutations(
    body: Mapping[str, Any],
) -> Iterator[tuple[str, dict[str, Any]]]:
    """Yield one type-valid, digest-sensitive mutation for every body member."""

    if set(body) != ROOT6_BODY_KEYS:
        raise AssertionError("root6 sensitivity body must contain exactly ten keys")
    replacements: dict[str, Any] = {
        "root_digest_version": 7,
        "project_id": f"{body['project_id']}.changed",
        "operating_mode": "mission_runtime.changed",
        "schema_contract": {
            **dict(body["schema_contract"]),
            "schema_version": int(body["schema_contract"]["schema_version"]) + 1,
        },
        "project_commit_no": int(body["project_commit_no"]) + 1,
        "canonical_authority_digest": _different_digest(
            body["canonical_authority_digest"], "f"
        ),
        "predecessor_project_commit_no": (
            None
            if body["predecessor_project_commit_no"] is not None
            else 0
        ),
        "predecessor_root_digest": (
            None
            if body["predecessor_root_digest"] is not None
            else _different_digest(body["predecessor_root_digest"], "e")
        ),
        "predecessor_transition_head_digest": (
            None
            if body["predecessor_transition_head_digest"] is not None
            else _different_digest(
                body["predecessor_transition_head_digest"], "d"
            )
        ),
        "transition_head_digest": (
            None
            if body["transition_head_digest"] is not None
            else _different_digest(body["transition_head_digest"], "c")
        ),
    }
    for key in ROOT6_BODY_KEYS:
        changed = dict(body)
        changed[key] = replacements[key]
        yield key, changed


class TestSourceBindingError(RuntimeError):
    """Raised when neither Git nor the sealed worker source bundle proves identity."""


def resolve_verified_test_source_commit(repo_root: Path | str) -> tuple[str, bool]:
    """Return ``(commit, archive_bound)`` for a checkout or sealed worker archive."""

    root = Path(repo_root).resolve()
    result = subprocess.run(
        ["git", "-C", str(root), "rev-parse", "HEAD"],
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    checkout_commit = result.stdout.strip().lower()
    if result.returncode == 0 and _SOURCE_COMMIT_RE.fullmatch(checkout_commit):
        return checkout_commit, False

    request_path = root.parent / "request.json"
    manifest_path = root.parent / "source-manifest.json"
    try:
        request = loads_strict_json_bytes(request_path.read_bytes())
        manifest_raw = manifest_path.read_bytes()
    except (OSError, UnicodeDecodeError, ValueError) as exc:
        raise TestSourceBindingError(
            "source identity is unavailable from Git or a sealed worker bundle"
        ) from exc
    if not isinstance(request, Mapping):
        raise TestSourceBindingError("worker request must be a JSON object")
    commit = request.get("source_commit")
    expected_manifest_digest = request.get("source_tree_manifest_sha256")
    if (
        not isinstance(commit, str)
        or _SOURCE_COMMIT_RE.fullmatch(commit) is None
        or request.get("source_delivery") != "exact_pushed_commit_archive"
        or request.get("controller_worktree_included") is not False
        or not isinstance(expected_manifest_digest, str)
        or hashlib.sha256(manifest_raw).hexdigest() != expected_manifest_digest
    ):
        raise TestSourceBindingError("worker request does not bind an exact source archive")

    repository_root = Path(__file__).resolve().parents[3]
    remote_compute_packages = repository_root / "packages" / "remote-compute"
    verifier_environment = {
        key: value
        for key in (
            "HOME",
            "LANG",
            "LC_ALL",
            "PATH",
            "SYSTEMROOT",
            "TEMP",
            "TMP",
            "TMPDIR",
            "WINDIR",
        )
        if (value := os.environ.get(key)) is not None
    }
    verifier_environment["PYTHONPATH"] = os.pathsep.join(
        (str(repository_root), str(remote_compute_packages))
    )
    verifier_environment["PYTHONSAFEPATH"] = "1"
    verifier_environment["PYTHONDONTWRITEBYTECODE"] = "1"
    verifier_code = (
        "import json,sys;"
        "from pathlib import Path;"
        "from remote_compute.source import verify_source_tree;"
        "print(json.dumps(verify_source_tree(Path(sys.argv[1]),Path(sys.argv[2])),"
        "sort_keys=True,separators=(',',':')))"
    )
    try:
        verification = subprocess.run(
            [sys.executable, "-c", verifier_code, str(root), str(manifest_path)],
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=verifier_environment,
            timeout=120,
        )
        verified = loads_strict_json_bytes(verification.stdout.encode("utf-8"))
    except (OSError, UnicodeDecodeError, ValueError, subprocess.TimeoutExpired) as exc:
        raise TestSourceBindingError("worker source tree verification failed") from exc
    if verification.returncode != 0 or not isinstance(verified, Mapping):
        raise TestSourceBindingError("worker source tree verification failed")
    if (
        verified.get("clean") is not True
        or verified.get("source_commit") != commit
        or verified.get("manifest_sha256") != expected_manifest_digest
    ):
        raise TestSourceBindingError("worker source tree identity does not match its request")
    return commit, True
