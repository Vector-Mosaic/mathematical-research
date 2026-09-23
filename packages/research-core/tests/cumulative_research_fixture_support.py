"""Offline preparation for one frozen behavioral case, never a research runner.

Only the returned provider packets may enter a future research request. The
controller separately installs store_seed through the existing owner APIs and
resolves fixture aliases to the exact references those writes return. This
module neither impersonates a Store nor invokes a model, service, or tool.
The evaluator rubric is deliberately not read by this module.
"""

from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path
from typing import Any


FIXTURE_ROOT = Path(__file__).resolve().parent / "fixtures" / "research_workspace"
MANIFEST_PATH = FIXTURE_ROOT / "cumulative_qualification_manifest.json"


def _read_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def load_visible_case(
    case_id: str, *, include_held_out: bool = False
) -> dict[str, Any]:
    """Prepare one case without exposing the rubric or other cases.

    include_held_out is an explicit fixture-author/evaluator selection, not
    authorization to run an experiment. Filesystem/tool isolation of an actual
    provider is a separate mandatory precondition in the manifest.
    """
    manifest = _read_json(MANIFEST_PATH)
    matches = [case for case in manifest["cases"] if case["case_id"] == case_id]
    if len(matches) != 1:
        raise ValueError("fixture case is absent or ambiguous")
    spec = matches[0]
    if spec["held_out"] and not include_held_out:
        raise ValueError("held-out fixture is unavailable to candidate tuning")
    if spec["input_kind"] == "synthetic":
        path = FIXTURE_ROOT / manifest["files"][spec["input_file"]]
        case = copy.deepcopy(_read_json(path)["cases"][case_id])
        initial = case["initial"]
        contribution = initial.pop("new_contribution")
        return {
            "provider_initial": initial,
            "provider_incoming": {"contribution": contribution},
            "store_seed": case["archive"],
            "initial_source_aliases": case["initial_source_aliases"],
        }
    raise ValueError("unsupported fixture input kind")


def comparison_cells() -> list[dict[str, Any]]:
    """Return the proposed synthetic comparison cells as data; do not schedule or execute them."""
    manifest = _read_json(MANIFEST_PATH)
    arms = manifest["comparison"]["arms"]
    cells = []
    for case_index, case in enumerate(manifest["cases"]):
        for replica in range(1, manifest["comparison"]["replicas_per_case_per_arm"] + 1):
            order = arms if (case_index + replica) % 2 == 0 else list(reversed(arms))
            for arm in order:
                cells.append(
                    {
                        "case_id": case["case_id"],
                        "replica": replica,
                        "arm": arm,
                        "cell_id": f"{case['case_id']}.{replica}.{arm}",
                        "status": "not_authorized_not_run",
                        "fresh_state_required": True,
                    }
                )
    return cells
