#!/usr/bin/env python3
"""Scripted, credential-free example using the actual durable research core.

The judgments below are supplied by the example's authors. This is neither a
model run nor a historical replay. ``run`` can only create a new output directory;
it has no attach, resume, overwrite or installed-Host mutation mode.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_EXAMPLE = "finite-free-localization"
EXAMPLES = {
    "finite-free-localization": {"timeout": 60, "extra_materials": ()},
    "loewner-obstruction": {"timeout": 600, "extra_materials": ("requirements.txt",)},
}
MARKER = "scripted-research-example.json"
MODE = "authored-scripted-walkthrough-no-model"
sys.path[:0] = [str(ROOT / "packages/research-core"),
               str(ROOT / "packages/research-attempt-adapter")]

from research_core.canonical_snapshot import CANONICAL_STATE_REPO_PATH, load_canonical_snapshot
from research_core.evidence_store import EvidenceCAS
from research_core.mission_evidence import (
    RawCaptureArtifactInput, commit_raw_capture, prepare_raw_capture,
    read_evidence_meaning, read_raw_capture_artifact,
)
from research_core.mission_interface import MissionInterface
from research_core.mission_operation_contract import MISSION_SEMANTIC_REQUEST_SCHEMA_VERSION
from research_core.research_model import deep_thaw
from research_core.workspace_paths import attest_current_principal


def dump(path: Path, value: object) -> None:
    with path.open("x", encoding="utf-8", newline="\n") as stream:
        json.dump(value, stream, indent=2, sort_keys=True, allow_nan=False)
        stream.write("\n")


def digest(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def canonical_source_commit() -> str:
    record_path = ROOT / ".mathematical-research-release.json"
    if record_path.exists():
        from research_core.mission_attempt_runtime import installed_release_source_digest
        record = json.loads(record_path.read_text(encoding="utf-8"))
        installed_release_source_digest(ROOT, record["release_sha"])
        return record["release_sha"]
    commit = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip()
    actual = (ROOT / CANONICAL_STATE_REPO_PATH).read_bytes()
    committed = subprocess.check_output(
        ["git", "show", f"{commit}:{CANONICAL_STATE_REPO_PATH}"], cwd=ROOT)
    if actual.replace(b"\r\n", b"\n") != committed.replace(b"\r\n", b"\n"):
        raise ValueError("canonical starting data must match the current Git commit")
    return commit


def execute(interface: MissionInterface, epoch: str, operation: str, body: dict) -> dict:
    response = deep_thaw(interface.execute_semantic_operation({
        "schema_version": MISSION_SEMANTIC_REQUEST_SCHEMA_VERSION,
        "operation": operation, "input": body,
    }, executive_epoch_id=epoch))
    if response["status"] != "completed":
        raise RuntimeError(f"{operation} failed: {json.dumps(response.get('error'))}")
    return response["result"]


def inspect(output: Path, *, example: str | None = None, details: bool = False) -> dict:
    """Open only this example's persisted store and read its exact records."""
    manifest = json.loads((output / MARKER).read_text(encoding="utf-8"))
    selected = manifest["example"]
    if manifest["mode"] != MODE or selected not in EXAMPLES:
        raise ValueError("output is not this scripted example")
    if example is not None and example != selected:
        raise ValueError("example selector does not match the stored walkthrough")
    interface = MissionInterface.open(output / "workspace", manifest["project_id"],
                                      manifest["mission_id"])
    store = interface._store
    cas = EvidenceCAS(store.paths)
    raw = []
    for item in manifest["raw_artifacts"]:
        retained = read_raw_capture_artifact(
            store, cas=cas, mission_id=manifest["mission_id"],
            capture_id=manifest["capture_id"], artifact_ordinal=item["ordinal"],
        )
        if digest(retained.content_bytes) != item["sha256"]:
            raise RuntimeError(f"retained bytes changed: {item['name']}")
        raw.append({"name": item["name"], "sha256": item["sha256"]})
    candidates = []
    for item in manifest["candidate_revisions"]:
        retained = deep_thaw(store.read_candidate_revision(
            mission_id=manifest["mission_id"], candidate_id=item["id"],
            revision=item["revision"],
        ))
        payload = retained["payload"]
        actual = digest(json.dumps(payload, sort_keys=True, separators=(",", ":"),
                                   allow_nan=False).encode("utf-8"))
        if actual != item["payload_sha256"]:
            raise RuntimeError(f"retained candidate changed: {item['id']}")
        candidates.append({"id": item["id"], "revision": item["revision"],
                           "statement": payload["exact_statement"],
                           "standing": payload["standing"],
                           **({"document": payload} if details else {})})
    meaning = read_evidence_meaning(store, evidence_id=manifest["evidence_id"], revision=1)
    if meaning.evidence.canonical_effect != "none":
        raise RuntimeError("scripted evidence unexpectedly claims a canonical effect")
    if {(s.capture_id, s.artifact_ordinal) for s in meaning.sources} != {
        (manifest["capture_id"], item["ordinal"]) for item in manifest["raw_artifacts"]
    }:
        raise RuntimeError("interpretation no longer refers to the exact supplied material")
    checkpoint = store.read_continuation_checkpoint(executive_epoch_id=manifest["epoch_id"])
    if checkpoint is None or checkpoint["project_commit_no"] != manifest["checkpoint_commit"]:
        raise RuntimeError("expected durable checkpoint was not found")
    result = {
        "example": selected, "mode": MODE,
        "workspace": str(output / "workspace"),
        "mathematical_check": manifest["mathematical_check"],
        "retained_raw_artifacts": raw, "candidate_revisions": candidates,
        "evidence_id": manifest["evidence_id"], "canonical_effect": "none",
        "checkpoint_commit": manifest["checkpoint_commit"],
        "reopened_store_checks": "passed",
        "demonstrates": "Exact supplied bytes, scoped judgments and their dependencies survive reopening.",
        "does_not_demonstrate": "Autonomous discovery, a fresh model successor, historical execution, canonical admission or RH.",
    }
    if details:
        result["evidence"] = meaning.evidence.to_payload()
        result["checkpoint"] = deep_thaw(checkpoint)
    return result


def run(output: Path, *, example: str = DEFAULT_EXAMPLE) -> dict:
    if example not in EXAMPLES:
        raise ValueError("unknown scripted example")
    source = ROOT / "examples" / example
    settings = EXAMPLES[example]
    if sys.platform != "linux":
        raise ValueError("the durable walkthrough requires Linux (the supported core runtime)")
    if output.exists() or output.is_symlink():
        raise ValueError("output must not exist; choose a new directory (nothing is deleted)")
    seed = json.loads((source / "mission.json").read_text(encoding="utf-8"))
    authored = json.loads((source / "walkthrough.json").read_text(encoding="utf-8"))
    source_commit = canonical_source_commit()
    loaded = load_canonical_snapshot(ROOT, source_commit=source_commit)
    if not loaded.ok or loaded.value is None:
        raise RuntimeError(str(loaded.failure))
    check = subprocess.run([sys.executable, str(source / "check.py")], cwd=ROOT,
                           check=True, stdout=subprocess.PIPE, timeout=settings["timeout"])
    check_result = json.loads(check.stdout)
    if not isinstance(check_result, dict):
        raise ValueError("mathematical checker must return one JSON object")
    # Atomic creation prevents this command from attaching to existing state.
    output.mkdir(parents=True, exist_ok=False)
    author_workspace = output / "scripted-author"
    author_workspace.mkdir()
    interface = MissionInterface.initialize_from_owner(
        output / "workspace", project_id=seed["project_id"],
        mission_id=seed["mission"]["mission_id"], canonical_snapshot=loaded.value,
        genesis_seed=seed, owner_command_id="public-scripted-example.bootstrap",
    )
    epoch = interface.authorize_executive_epoch_from_owner({
        "expected_cut": interface.host_snapshot()["authorization_cut"],
    })["executive_epoch_id"]
    interface.bind_executive_epoch_from_owner({
        "executiveEpochId": epoch, "rootThreadId": "scripted-author:no-model-thread",
        "workspaceRoot": str(author_workspace),
    })
    store = interface._store
    cas = EvidenceCAS(store.paths)
    materials = [(name, (source / name).read_bytes()) for name in
                 ("problem.json", "mathematics.md", "check.py", "walkthrough.json")
                 + settings["extra_materials"]]
    materials.append(("checker-output.json", check.stdout))
    capture = prepare_raw_capture(
        store, mission_id=seed["mission"]["mission_id"], executive_epoch_id=epoch,
        capture_kind="output", observation_id="public-example.authored-material",
        assignment_id="scripted-author:no-model-assignment",
        provenance={"kind": MODE, "supplied_content": True,
                    "checker_execution": "local-python-subprocess", "historical_replay": False},
        completion={"lifecycle": "completed"},
        artifacts=tuple(RawCaptureArtifactInput(
            role="source" if name != "checker-output.json" else "result", logical_name=name,
            content_bytes=body, media_type="application/json" if name.endswith(".json") else "text/plain",
            encoding="utf-8",
        ) for name, body in materials),
    )
    commit_raw_capture(store, cas=cas, record=capture,
                       lease=store.reissue_writer_lease(attest_current_principal()),
                       actor="public-example-scripted-author")
    evidence = dict(authored["evidence"])
    evidence["capture_scopes"] = [{
        "captured_material_id": f"capture:{capture.capture_id}", "artifact_ordinal": ordinal,
        "exact_scope": f"Complete supplied {name}; authored material and new checker output, not a model transcript.",
        "coverage": "complete_artifact",
    } for ordinal, (name, _) in enumerate(materials)]
    execute(interface, epoch, "interpret_material", evidence)
    revisions = []
    for candidate in authored["candidate_steps"]:
        result = execute(interface, epoch, "record_candidate", candidate)
        identity = candidate["candidate_id"].split(":", 1)[1]
        stored = deep_thaw(store.read_candidate_revision(
            mission_id=seed["mission"]["mission_id"], candidate_id=identity,
            revision=result["revision"],
        ))
        revisions.append({"id": identity, "revision": result["revision"],
                          "payload_sha256": digest(json.dumps(stored["payload"], sort_keys=True,
                          separators=(",", ":"), allow_nan=False).encode("utf-8"))})
    execute(interface, epoch, "record_strategy", authored["strategy"])
    execute(interface, epoch, "checkpoint", {})
    checkpoint = store.read_continuation_checkpoint(executive_epoch_id=epoch)
    if checkpoint is None:
        raise RuntimeError("checkpoint operation did not persist its result")
    dump(output / MARKER, {
        "example": example, "mode": MODE, "canonical_source_commit": source_commit,
        "project_id": seed["project_id"], "mission_id": seed["mission"]["mission_id"],
        "epoch_id": epoch, "capture_id": capture.capture_id,
        "raw_artifacts": [{"ordinal": n, "name": name, "sha256": digest(body)}
                          for n, (name, body) in enumerate(materials)],
        "candidate_revisions": revisions,
        "evidence_id": evidence["evidence_id"].split(":", 1)[1],
        "checkpoint_commit": checkpoint["project_commit_no"], "mathematical_check": check_result,
    })
    return inspect(output, example=example)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("run", "inspect"))
    parser.add_argument("--example", choices=tuple(EXAMPLES),
                        help="run defaults to finite-free-localization; inspect reads the stored selector")
    parser.add_argument("--output", required=True, type=Path,
                        help="new directory for run; previously created directory for inspect")
    parser.add_argument("--details", action="store_true", help="inspect full retained candidate/evidence/checkpoint documents")
    args = parser.parse_args()
    try:
        output = args.output.expanduser().resolve()
        result = (run(output, example=args.example or DEFAULT_EXAMPLE) if args.command == "run"
                  else inspect(output, example=args.example, details=args.details))
        print(json.dumps(result, indent=2, sort_keys=True, allow_nan=False))
        return 0
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as exc:
        print(f"research example failed: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
