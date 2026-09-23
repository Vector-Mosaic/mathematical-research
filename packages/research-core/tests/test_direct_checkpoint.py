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
    DirectCheckpointError,
    DirectContinuationCheckpoint,
    OwnerRevisionRef,
    ResolvedOwnerRevision,
    owner_revision_ref_from_mapping,
    prepare_direct_continuation_checkpoint,
)
from research_core.research_model import deep_thaw  # noqa: E402


MISSION_ID = "mission.rh"


def _digest(document: Mapping[str, Any]) -> str:
    return hashlib.sha256(canonical_json_bytes(document)).hexdigest()


def _document(
    kind: str,
    identity: str,
    revision: int = 1,
    *,
    mission_id: str = MISSION_ID,
    extra: Mapping[str, Any] | None = None,
) -> Mapping[str, Any]:
    return {
        "owner_kind": kind,
        "owner_identity": identity,
        "owner_revision": revision,
        "mission_id": mission_id,
        **dict(extra or {}),
    }


def _ref(
    kind: str,
    identity: str,
    revision: int,
    document: Mapping[str, Any],
) -> OwnerRevisionRef:
    return OwnerRevisionRef(kind, identity, revision, _digest(document))


def _resolved(
    reference: OwnerRevisionRef,
    document: Mapping[str, Any],
    *,
    mission_id: str = MISSION_ID,
    outgoing: Sequence[OwnerRevisionRef] = (),
    current: bool = False,
) -> ResolvedOwnerRevision:
    return ResolvedOwnerRevision(
        reference=reference,
        mission_id=mission_id,
        validated_document=document,
        outgoing_refs=tuple(outgoing),
        is_current_head=current,
    )


class _Resolver:
    def __init__(
        self,
        rows: Sequence[ResolvedOwnerRevision],
    ) -> None:
        self._rows = MappingProxyType(
            {row.reference.revision_identity: row for row in rows}
        )
        self.calls: list[tuple[str, str, int]] = []

    def __call__(
        self,
        reference: OwnerRevisionRef,
    ) -> ResolvedOwnerRevision | None:
        self.calls.append(reference.revision_identity)
        return self._rows.get(reference.revision_identity)


def _root_rows(
    *,
    mission_id: str = MISSION_ID,
    strategy_id: str = "strategy.rh",
    mission_current: bool = True,
    strategy_current: bool = True,
    strategy_outgoing: Sequence[OwnerRevisionRef] = (),
) -> tuple[
    OwnerRevisionRef,
    OwnerRevisionRef,
    ResolvedOwnerRevision,
    ResolvedOwnerRevision,
]:
    mission_document = _document(
        "mission",
        mission_id,
        mission_id=mission_id,
    )
    mission_ref = _ref("mission", mission_id, 1, mission_document)
    strategy_document = _document(
        "strategy",
        strategy_id,
        mission_id=mission_id,
        extra={"mission_continuation": "continue"},
    )
    strategy_ref = _ref("strategy", strategy_id, 1, strategy_document)
    return (
        mission_ref,
        strategy_ref,
        _resolved(
            mission_ref,
            mission_document,
            mission_id=mission_id,
            current=mission_current,
        ),
        _resolved(
            strategy_ref,
            strategy_document,
            mission_id=mission_id,
            outgoing=strategy_outgoing,
            current=strategy_current,
        ),
    )


def _prepare(
    mission_ref: OwnerRevisionRef,
    strategy_ref: OwnerRevisionRef,
    rows: Sequence[ResolvedOwnerRevision],
    *,
    epoch: str = "epoch.1",
    causal: Sequence[Mapping[str, Any]] = (),
    unresolved: Sequence[Mapping[str, Any]] = (),
    captures: Sequence[Mapping[str, Any]] = (),
):
    return prepare_direct_continuation_checkpoint(
        mission_root=mission_ref,
        strategy_root=strategy_ref,
        resolver=_Resolver(rows),
        causal_pointers=causal,
        unresolved_pointers=unresolved,
        pending_capture_locators=captures,
        predecessor_checkpoint=None,
        authoring_epoch_id=epoch,
        project_commit=17,
        schema_version=1,
    )


class DirectCheckpointTests(unittest.TestCase):
    def test_compact_v2_retains_only_current_boundary_facts(self) -> None:
        mission_ref, strategy_ref, _mission_row, _strategy_row = _root_rows()
        checkpoint = prepare_direct_continuation_checkpoint(
            mission_root=mission_ref,
            strategy_root=strategy_ref,
            unresolved_pointers=(),
            predecessor_checkpoint=None,
            authoring_epoch_id="epoch.compact",
            project_commit=17,
        )

        self.assertEqual(checkpoint.document["schema_version"], 2)
        self.assertEqual(
            set(checkpoint.document),
            {
                "schema_version",
                "kind",
                "checkpoint_id",
                "mission_root",
                "strategy_root",
                "unresolved_pointers",
                "predecessor_checkpoint",
                "authoring_epoch_id",
                "project_commit",
            },
        )
        self.assertLess(len(canonical_json_bytes(checkpoint.document)), 1024)
        reconstructed = DirectContinuationCheckpoint(
            deep_thaw(checkpoint.document),
            checkpoint.digest_sha256,
        )
        self.assertEqual(reconstructed, checkpoint)
        with self.assertRaisesRegex(ValueError, "derives closure"):
            prepare_direct_continuation_checkpoint(
                mission_root=mission_ref,
                strategy_root=strategy_ref,
                resolver=lambda _reference: None,
                predecessor_checkpoint=None,
                authoring_epoch_id="epoch.compact.invalid",
                project_commit=18,
            )

    def test_large_current_root_cut_is_proof_neutral_and_has_no_added_cap(self) -> None:
        evidence_rows: list[ResolvedOwnerRevision] = []
        evidence_refs: list[OwnerRevisionRef] = []
        for ordinal in range(300):
            identity = f"evidence.{ordinal:03d}"
            document = _document("evidence", identity)
            reference = _ref("evidence", identity, 1, document)
            evidence_refs.append(reference)
            evidence_rows.append(_resolved(reference, document))

        mission_ref, strategy_ref, mission_row, strategy_row = _root_rows(
            strategy_outgoing=tuple(reversed(evidence_refs))
        )
        captures = [
            {"capture_id": f"capture.{ordinal:03d}", "artifact_ordinal": ordinal}
            for ordinal in reversed(range(40))
        ]
        checkpoint = _prepare(
            mission_ref,
            strategy_ref,
            (mission_row, strategy_row, *evidence_rows),
            causal=(
                {
                    "owner_ref": strategy_ref.to_mapping(),
                    "json_pointer": "",
                },
            ),
            captures=captures,
        )

        self.assertEqual(
            len(checkpoint.document["transitive_owner_refs"]),
            len(evidence_refs),
        )
        self.assertEqual(
            [item["capture_id"] for item in checkpoint.document["pending_capture_locators"]],
            sorted(item["capture_id"] for item in captures),
        )
        self.assertEqual(
            set(checkpoint.document),
            {
                "schema_version",
                "kind",
                "checkpoint_id",
                "mission_root",
                "strategy_root",
                "transitive_owner_refs",
                "causal_pointers",
                "unresolved_pointers",
                "pending_capture_locators",
                "predecessor_checkpoint",
                "authoring_epoch_id",
                "project_commit",
            },
        )
        forbidden = {
            "budget",
            "cap",
            "completeness",
            "limit",
            "quota",
            "receipt",
            "score",
        }
        self.assertTrue(forbidden.isdisjoint(checkpoint.document))
        with self.assertRaises(TypeError):
            checkpoint.document["kind"] = "changed"

    def test_historical_mission_or_strategy_root_is_rejected(self) -> None:
        for mission_current, strategy_current in ((False, True), (True, False)):
            with self.subTest(
                mission_current=mission_current,
                strategy_current=strategy_current,
            ):
                mission_ref, strategy_ref, mission_row, strategy_row = _root_rows(
                    mission_current=mission_current,
                    strategy_current=strategy_current,
                )
                with self.assertRaises(DirectCheckpointError) as caught:
                    _prepare(
                        mission_ref,
                        strategy_ref,
                        (mission_row, strategy_row),
                    )
                self.assertEqual(
                    caught.exception.code,
                    "checkpoint_root_historical",
                )

    def test_disconnected_pointer_cannot_expand_the_owner_cut(self) -> None:
        disconnected_document = _document("evidence", "evidence.disconnected")
        disconnected_ref = _ref(
            "evidence",
            "evidence.disconnected",
            1,
            disconnected_document,
        )
        disconnected_row = _resolved(disconnected_ref, disconnected_document)
        mission_ref, strategy_ref, mission_row, strategy_row = _root_rows()
        resolver = _Resolver((mission_row, strategy_row, disconnected_row))

        with self.assertRaises(DirectCheckpointError) as caught:
            prepare_direct_continuation_checkpoint(
                mission_root=mission_ref,
                strategy_root=strategy_ref,
                resolver=resolver,
                causal_pointers=(
                    {
                        "owner_ref": disconnected_ref.to_mapping(),
                        "json_pointer": "",
                    },
                ),
                predecessor_checkpoint=None,
                authoring_epoch_id="epoch.disconnected",
                project_commit=2,
                schema_version=1,
            )
        self.assertEqual(
            caught.exception.code,
            "checkpoint_pointer_outside_closure",
        )
        self.assertNotIn(disconnected_ref.revision_identity, resolver.calls)

    def test_context_document_is_not_parsed_for_external_owner_refs(self) -> None:
        external_document = _document("evidence", "evidence.external")
        external_ref = _ref(
            "evidence",
            "evidence.external",
            1,
            external_document,
        )
        context_document = _document(
            "context",
            "context.external",
            extra={"external_reference": deep_thaw(external_ref.to_mapping())},
        )
        context_ref = _ref("context", "context.external", 1, context_document)
        context_row = _resolved(context_ref, context_document, outgoing=())
        mission_ref, strategy_ref, mission_row, strategy_row = _root_rows(
            strategy_outgoing=(context_ref,)
        )

        checkpoint = _prepare(
            mission_ref,
            strategy_ref,
            (mission_row, strategy_row, context_row),
        )
        transitive = tuple(
            owner_revision_ref_from_mapping(item)
            for item in checkpoint.document["transitive_owner_refs"]
        )
        self.assertEqual(transitive, (context_ref,))
        self.assertNotIn(external_ref, transitive)

    def test_canonical_hash_identity_separates_colon_ambiguous_inputs(self) -> None:
        first = _root_rows(
            mission_id="a:b",
            strategy_id="strategy.first",
        )
        second = _root_rows(
            mission_id="a",
            strategy_id="strategy.second",
        )
        first_checkpoint = _prepare(
            first[0],
            first[1],
            first[2:],
            epoch="c",
        )
        second_checkpoint = _prepare(
            second[0],
            second[1],
            second[2:],
            epoch="b:c",
        )

        self.assertNotEqual(
            first_checkpoint.document["checkpoint_id"],
            second_checkpoint.document["checkpoint_id"],
        )
        self.assertRegex(
            first_checkpoint.document["checkpoint_id"],
            r"^checkpoint:[0-9a-f]{64}$",
        )

    def test_cycle_keeps_historical_revisions_and_digest_conflict_rejects(self) -> None:
        first_document = _document("evidence", "evidence.shared", 1)
        second_document = _document("evidence", "evidence.shared", 2)
        first_ref = _ref("evidence", "evidence.shared", 1, first_document)
        second_ref = _ref("evidence", "evidence.shared", 2, second_document)
        first_row = _resolved(first_ref, first_document, outgoing=(second_ref,))
        second_row = _resolved(second_ref, second_document, outgoing=(first_ref,))
        mission_ref, strategy_ref, mission_row, strategy_row = _root_rows(
            strategy_outgoing=(second_ref,)
        )

        checkpoint = _prepare(
            mission_ref,
            strategy_ref,
            (mission_row, strategy_row, first_row, second_row),
        )
        transitive = tuple(
            owner_revision_ref_from_mapping(item)
            for item in checkpoint.document["transitive_owner_refs"]
        )
        self.assertEqual(transitive, (first_ref, second_ref))

        conflicting_first = OwnerRevisionRef(
            "evidence",
            "evidence.shared",
            1,
            "f" * 64,
        )
        conflicting_second_row = _resolved(
            second_ref,
            second_document,
            outgoing=(conflicting_first,),
        )
        conflict_roots = _root_rows(
            strategy_outgoing=(first_ref, second_ref)
        )
        with self.assertRaises(DirectCheckpointError) as caught:
            _prepare(
                conflict_roots[0],
                conflict_roots[1],
                (
                    conflict_roots[2],
                    conflict_roots[3],
                    first_row,
                    conflicting_second_row,
                ),
            )
        self.assertEqual(
            caught.exception.code,
            "checkpoint_reference_digest_conflict",
        )

    def test_checkpoint_value_normalizes_and_rejects_internal_inconsistency(self) -> None:
        evidence_document = _document("evidence", "evidence.one")
        evidence_ref = _ref("evidence", "evidence.one", 1, evidence_document)
        evidence_row = _resolved(evidence_ref, evidence_document)
        mission_ref, strategy_ref, mission_row, strategy_row = _root_rows(
            strategy_outgoing=(evidence_ref,)
        )
        checkpoint = _prepare(
            mission_ref,
            strategy_ref,
            (mission_row, strategy_row, evidence_row),
        )
        reconstructed = DirectContinuationCheckpoint(
            document=deep_thaw(checkpoint.document),
            digest_sha256=checkpoint.digest_sha256,
        )
        reconstructed.verify_integrity()
        self.assertEqual(reconstructed, checkpoint)

        duplicate_ref = deep_thaw(checkpoint.document)
        duplicate_ref["transitive_owner_refs"].append(
            deep_thaw(evidence_ref.to_mapping())
        )
        duplicate_capture = deep_thaw(checkpoint.document)
        duplicate_capture["pending_capture_locators"] = [
            {"capture_id": "capture.1", "artifact_ordinal": 0},
            {"capture_id": "capture.1", "artifact_ordinal": 0},
        ]
        malformed_pointer = deep_thaw(checkpoint.document)
        malformed_pointer["causal_pointers"] = [
            {
                "owner_ref": {
                    **deep_thaw(strategy_ref.to_mapping()),
                    "copied_meaning": "forbidden",
                },
                "json_pointer": "",
            }
        ]
        outside_pointer = deep_thaw(checkpoint.document)
        outside_document = _document("evidence", "evidence.outside")
        outside_ref = _ref("evidence", "evidence.outside", 1, outside_document)
        outside_pointer["unresolved_pointers"] = [
            {
                "owner_ref": deep_thaw(outside_ref.to_mapping()),
                "json_pointer": "",
            }
        ]
        stale_checkpoint_id = deep_thaw(checkpoint.document)
        stale_checkpoint_id["checkpoint_id"] = "checkpoint:" + ("0" * 64)
        boolean_commit = deep_thaw(checkpoint.document)
        boolean_commit["project_commit"] = True
        for malformed in (
            duplicate_ref,
            duplicate_capture,
            malformed_pointer,
            outside_pointer,
            stale_checkpoint_id,
            boolean_commit,
        ):
            with self.subTest(keys=tuple(sorted(malformed))):
                with self.assertRaises(ValueError):
                    DirectContinuationCheckpoint(
                        document=malformed,
                        digest_sha256=_digest(malformed),
                    )

        with self.assertRaisesRegex(ValueError, "digest is stale"):
            replace(checkpoint, digest_sha256="0" * 64)

        replaced = deep_thaw(checkpoint.document)
        replaced["pending_capture_locators"] = [
            {"capture_id": "capture.unverified", "artifact_ordinal": 0}
        ]
        structurally_valid = replace(
            checkpoint,
            document=replaced,
            digest_sha256=_digest(replaced),
        )
        structurally_valid.verify_integrity()
        self.assertEqual(
            structurally_valid.document["pending_capture_locators"],
            ({"capture_id": "capture.unverified", "artifact_ordinal": 0},),
        )


if __name__ == "__main__":
    unittest.main()
