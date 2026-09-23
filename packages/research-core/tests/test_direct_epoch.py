from __future__ import annotations

import hashlib
import sys
import unittest
from dataclasses import replace
from pathlib import Path
from types import MappingProxyType
from typing import Any, Mapping, Sequence


PACKAGE_ROOT = Path(__file__).resolve().parents[1]
if str(PACKAGE_ROOT) not in sys.path:
    sys.path.insert(0, str(PACKAGE_ROOT))

from research_core.json_support import canonical_json_bytes  # noqa: E402
from research_core.mission_executive import (  # noqa: E402
    DirectContinuationCheckpoint,
    DirectExecutiveEpochAuthority,
    DirectExecutiveEpochEvent,
    OwnerRevisionRef,
    ResolvedOwnerRevision,
    parse_direct_executive_epoch_event,
    prepare_direct_continuation_checkpoint,
    prepare_direct_executive_epoch_authorized_event,
    prepare_direct_executive_epoch_bound_event,
    prepare_direct_executive_epoch_checkpointed_event,
    prepare_direct_executive_epoch_failed_before_checkpoint_event,
    reissue_direct_executive_epoch_authority,
)
from research_core.research_model import deep_thaw  # noqa: E402


MISSION_ID = "mission.rh"
EPOCH_ID = "epoch.rh.1"


def _digest(value: Any) -> str:
    return hashlib.sha256(canonical_json_bytes(value)).hexdigest()


def _owner(
    kind: str,
    identity: str,
    *,
    current: bool,
) -> ResolvedOwnerRevision:
    document = {
        "owner_kind": kind,
        "owner_identity": identity,
        "mission_id": MISSION_ID,
    }
    reference = OwnerRevisionRef(kind, identity, 1, _digest(document))
    return ResolvedOwnerRevision(
        reference=reference,
        mission_id=MISSION_ID,
        validated_document=document,
        is_current_head=current,
    )


def _checkpoint(*, epoch_id: str = EPOCH_ID) -> DirectContinuationCheckpoint:
    mission = _owner("mission", MISSION_ID, current=True)
    strategy = _owner("strategy", "strategy.rh", current=True)
    rows = MappingProxyType(
        {
            mission.reference.revision_identity: mission,
            strategy.reference.revision_identity: strategy,
        }
    )
    return prepare_direct_continuation_checkpoint(
        mission_root=mission.reference,
        strategy_root=strategy.reference,
        resolver=lambda reference: rows.get(reference.revision_identity),
        predecessor_checkpoint=None,
        authoring_epoch_id=epoch_id,
        project_commit=9,
        schema_version=1,
    )


def _authorized(
    *,
    epoch_id: str = EPOCH_ID,
    mission_id: str = MISSION_ID,
) -> DirectExecutiveEpochEvent:
    mission_document = {
        "owner_kind": "mission",
        "owner_identity": mission_id,
        "mission_id": mission_id,
    }
    mission_root = OwnerRevisionRef(
        "mission",
        mission_id,
        3,
        _digest(mission_document),
    )
    return prepare_direct_executive_epoch_authorized_event(
        executive_epoch_id=epoch_id,
        mission_root=mission_root,
        predecessor_checkpoint={
            "checkpoint_id": "checkpoint:prior",
            "payload_sha256": "1" * 64,
        },
    )


def _bound(
    *,
    epoch_id: str = EPOCH_ID,
    mission_id: str = MISSION_ID,
    goal_thread_id: str = "thread.rh.1",
    workspace_root: str = "C:/work/rh",
) -> DirectExecutiveEpochEvent:
    return prepare_direct_executive_epoch_bound_event(
        executive_epoch_id=epoch_id,
        mission_id=mission_id,
        goal_thread_id=goal_thread_id,
        workspace_root=workspace_root,
    )


def _readback(
    event: DirectExecutiveEpochEvent,
    *,
    ordinal: int,
    predecessor: DirectExecutiveEpochEvent | None,
) -> Mapping[str, Any]:
    return {
        "executive_epoch_id": event.executive_epoch_id,
        "event_ordinal": ordinal,
        "mission_id": event.mission_id,
        "project_commit_no": 20 + ordinal,
        "event_kind": event.event_kind,
        "event": deep_thaw(event.document),
        "event_digest": event.digest_sha256,
        "predecessor_event_ordinal": None if predecessor is None else ordinal - 1,
        "predecessor_event_digest": (
            None if predecessor is None else predecessor.digest_sha256
        ),
        "created_actor": "codex",
        "created_at": f"2026-08-23T12:00:0{ordinal}Z",
        "row_digest": _digest({"row": ordinal}),
    }


def _chain(
    authorized: DirectExecutiveEpochEvent | None = None,
    bound: DirectExecutiveEpochEvent | None = None,
) -> tuple[Mapping[str, Any], Mapping[str, Any]]:
    authorized = authorized or _authorized()
    bound = bound or _bound()
    return (
        _readback(authorized, ordinal=1, predecessor=None),
        _readback(bound, ordinal=2, predecessor=authorized),
    )


def _authority(
    event_readbacks: Sequence[Mapping[str, Any]] | None = None,
) -> DirectExecutiveEpochAuthority:
    return reissue_direct_executive_epoch_authority(
        event_readbacks=event_readbacks or _chain(),
        project_id="project.rh",
        root_identity="root.rh",
        canonical_authority_digest="a" * 64,
    )


class DirectExecutiveEpochTests(unittest.TestCase):
    def test_event_documents_have_exact_closed_command_shapes(self) -> None:
        authorized = _authorized()
        bound = _bound()
        checkpointed = prepare_direct_executive_epoch_checkpointed_event(
            _checkpoint()
        )
        failed = prepare_direct_executive_epoch_failed_before_checkpoint_event(
            executive_epoch_id=EPOCH_ID,
            mission_id=MISSION_ID,
            reconciliation={
                "stage": "goal_runtime",
                "failure_reason": "goal ended without a checkpoint",
            },
        )
        common = {
            "schema_version",
            "kind",
            "executive_epoch_id",
            "mission_id",
        }
        expected_specific = {
            "authorized": {"mission_root", "predecessor_checkpoint"},
            "bound": {"goal_thread_id", "workspace_root"},
            "checkpointed": {"checkpoint_ref"},
            "failed_before_checkpoint": {"reconciliation"},
        }
        for event in (authorized, bound, checkpointed, failed):
            with self.subTest(kind=event.event_kind):
                self.assertEqual(
                    set(event.document),
                    common | expected_specific[event.event_kind],
                )
                self.assertEqual(event.document["kind"], event.event_kind)
                self.assertEqual(event.document["schema_version"], 1)
                event.verify_integrity()
        self.assertEqual(
            checkpointed.document["checkpoint_ref"],
            _checkpoint().to_reference(),
        )

    def test_event_identity_digest_and_nested_shapes_fail_closed(self) -> None:
        authorized = _authorized()
        with self.assertRaises(ValueError):
            replace(authorized, digest_sha256="0" * 64)
        reconstructed = DirectExecutiveEpochEvent(
            document=deep_thaw(authorized.document),
            digest_sha256=authorized.digest_sha256,
        )
        reconstructed.verify_integrity()
        self.assertEqual(reconstructed, authorized)

        wrong_mission = deep_thaw(authorized.document)
        wrong_mission["mission_id"] = "mission.foreign"
        extra = deep_thaw(authorized.document)
        extra["receipt"] = "forbidden"
        for malformed in (wrong_mission, extra):
            with self.subTest(keys=tuple(sorted(malformed))):
                with self.assertRaises(ValueError):
                    parse_direct_executive_epoch_event(malformed)

        with self.assertRaises(TypeError):
            prepare_direct_executive_epoch_checkpointed_event(object())  # type: ignore[arg-type]
        with self.assertRaises(ValueError):
            prepare_direct_executive_epoch_failed_before_checkpoint_event(
                executive_epoch_id=EPOCH_ID,
                mission_id=MISSION_ID,
                reconciliation={},
            )

    def test_authority_projection_derives_only_from_exact_store_readback_chain(
        self,
    ) -> None:
        authorized = _authorized()
        bound = _bound()
        chain = _chain(authorized, bound)
        authority = _authority(chain)

        self.assertEqual(
            set(authority.material),
            {
                "project_id",
                "root_identity",
                "canonical_authority_digest",
                "executive_epoch_id",
                "mission_id",
                "mission_root",
                "predecessor_checkpoint",
                "bound_event_ordinal",
                "bound_event_digest",
                "goal_thread_id",
                "workspace_root",
            },
        )
        self.assertEqual(authority.project_id, "project.rh")
        self.assertEqual(authority.root_identity, "root.rh")
        self.assertEqual(authority.canonical_authority_digest, "a" * 64)
        self.assertEqual(authority.mission_id, MISSION_ID)
        self.assertEqual(authority.executive_epoch_id, EPOCH_ID)
        self.assertEqual(authority.mission_root, authorized.document["mission_root"])
        self.assertEqual(
            authority.predecessor_checkpoint,
            authorized.document["predecessor_checkpoint"],
        )
        self.assertEqual(authority.goal_thread_id, "thread.rh.1")
        self.assertEqual(authority.workspace_root, "C:/work/rh")
        self.assertEqual(authority.bound_event_ordinal, 2)
        self.assertEqual(authority.bound_event_digest, bound.digest_sha256)
        self.assertEqual(authority.authority_sha256, _digest(authority.material))
        authority.verify_integrity()

        extra = [dict(item) for item in chain]
        extra[0]["readback_receipt"] = "not Store vocabulary"
        reversed_chain = tuple(reversed(chain))
        wrong_predecessor = [dict(item) for item in chain]
        wrong_predecessor[1]["predecessor_event_digest"] = "f" * 64
        for malformed in (extra, reversed_chain, wrong_predecessor, chain[:1]):
            with self.subTest(length=len(malformed)):
                with self.assertRaises(ValueError):
                    _authority(malformed)

    def test_store_readback_digest_and_identity_mismatches_reject(self) -> None:
        chain = _chain()
        mutations = (
            ("event_digest", "f" * 64),
            ("executive_epoch_id", "epoch.foreign"),
            ("mission_id", "mission.foreign"),
            ("event_kind", "checkpointed"),
        )
        for key, value in mutations:
            malformed = [dict(item) for item in chain]
            malformed[0][key] = value
            with self.subTest(key=key):
                with self.assertRaises(ValueError):
                    _authority(malformed)

    def test_crossed_epoch_and_foreign_mission_chains_reject(self) -> None:
        crossed_epoch = _chain(_authorized(), _bound(epoch_id="epoch.other"))
        crossed_mission = _chain(
            _authorized(),
            _bound(mission_id="mission.foreign"),
        )
        for malformed in (crossed_epoch, crossed_mission):
            with self.subTest(bound=malformed[1]["event"]):
                with self.assertRaises(ValueError):
                    _authority(malformed)

    def test_authority_value_accepts_exact_content_and_rejects_stale_digest(self) -> None:
        authority = _authority()
        reconstructed = DirectExecutiveEpochAuthority(
            material=deep_thaw(authority.material),
            authority_sha256=authority.authority_sha256,
        )
        reconstructed.verify_integrity()
        self.assertEqual(reconstructed, authority)

        tampered_material = deep_thaw(authority.material)
        tampered_material["workspace_root"] = "C:/work/other"
        with self.assertRaisesRegex(ValueError, "digest is stale"):
            replace(authority, material=tampered_material)
        changed_value = replace(
            authority,
            material=tampered_material,
            authority_sha256=_digest(tampered_material),
        )
        changed_value.verify_integrity()
        self.assertEqual(changed_value.workspace_root, "C:/work/other")

        malformed = deep_thaw(authority.material)
        malformed["receipt"] = "forbidden"
        with self.assertRaisesRegex(ValueError, "wrong closed shape"):
            DirectExecutiveEpochAuthority(
                material=malformed,
                authority_sha256=_digest(malformed),
            )

    def test_large_values_add_no_caps_scores_receipts_or_evidence_head(self) -> None:
        long_workspace = "C:/work/" + ("rh/" * 5000)
        bound = _bound(workspace_root=long_workspace)
        failed = prepare_direct_executive_epoch_failed_before_checkpoint_event(
            executive_epoch_id=EPOCH_ID,
            mission_id=MISSION_ID,
            reconciliation={
                "stage": "goal_runtime",
                "failure_reason": "owner boundary failed",
            },
        )
        authority = _authority(_chain(_authorized(), bound))
        self.assertEqual(authority.workspace_root, long_workspace)
        self.assertEqual(
            failed.document["reconciliation"],
            {
                "stage": "goal_runtime",
                "failure_reason": "owner boundary failed",
            },
        )

        material = canonical_json_bytes(
            {
                "events": [bound.document, failed.document],
                "authority": authority.material,
            }
        ).decode("utf-8").lower()
        for forbidden in (
            "budget",
            "cap",
            "deadline",
            "evidence",
            "limit",
            "objective",
            "quota",
            "receipt",
            "score",
            "strategy",
            "timeout",
        ):
            with self.subTest(forbidden=forbidden):
                self.assertNotIn(forbidden, material)


if __name__ == "__main__":
    unittest.main()
