from __future__ import annotations

import hashlib
import subprocess
import sys
import unittest
from dataclasses import replace
from pathlib import Path
from typing import Any, Mapping


PACKAGE_ROOT = Path(__file__).resolve().parents[1]
if str(PACKAGE_ROOT) not in sys.path:
    sys.path.insert(0, str(PACKAGE_ROOT))

from research_core.context_revision import (  # noqa: E402
    ContextRevisionRecord,
    commit_context_revision,
    context_reference_projection,
    issue_context_revision,
    issue_context_revision_v2,
    issue_context_revision_v3,
    prepare_scientific_context_update,
    read_context_revision,
)
from research_core.json_support import canonical_json_bytes  # noqa: E402
from research_core.mission_executive import (  # noqa: E402
    DirectExecutiveEpochAuthority,
    OwnerRevisionRef,
    prepare_direct_executive_epoch_authorized_event,
    prepare_direct_executive_epoch_bound_event,
    reissue_direct_executive_epoch_authority,
)
from research_core.research_model import deep_thaw  # noqa: E402


MISSION_ID = "mission.context"
CONTEXT_ID = "context.direct"


def _digest(value: Any) -> str:
    return hashlib.sha256(canonical_json_bytes(value)).hexdigest()


def _authority(
    *,
    epoch_id: str = "epoch.context.1",
    canonical_authority_digest: str = "c" * 64,
) -> DirectExecutiveEpochAuthority:
    authorized = prepare_direct_executive_epoch_authorized_event(
        executive_epoch_id=epoch_id,
        mission_root=OwnerRevisionRef(
            "mission",
            MISSION_ID,
            1,
            "1" * 64,
        ),
        predecessor_checkpoint=None,
    )
    bound = prepare_direct_executive_epoch_bound_event(
        executive_epoch_id=epoch_id,
        mission_id=MISSION_ID,
        goal_thread_id=f"thread:{epoch_id}",
        workspace_root="C:/work/rh",
    )

    def readback(
        event: Any,
        ordinal: int,
        predecessor: Any,
    ) -> Mapping[str, Any]:
        return {
            "executive_epoch_id": event.executive_epoch_id,
            "event_ordinal": ordinal,
            "mission_id": event.mission_id,
            "project_commit_no": ordinal,
            "event_kind": event.event_kind,
            "event": deep_thaw(event.document),
            "event_digest": event.digest_sha256,
            "predecessor_event_ordinal": None if predecessor is None else 1,
            "predecessor_event_digest": (
                None if predecessor is None else predecessor.digest_sha256
            ),
            "created_actor": "coordinating-codex",
            "created_at": f"2026-08-24T12:00:0{ordinal}Z",
            "row_digest": str(ordinal) * 64,
        }

    return reissue_direct_executive_epoch_authority(
        event_readbacks=(
            readback(authorized, 1, None),
            readback(bound, 2, authorized),
        ),
        project_id="project.rh",
        root_identity="root.rh",
        canonical_authority_digest=canonical_authority_digest,
    )


def _record(
    authority: DirectExecutiveEpochAuthority,
    *,
    purpose: str = "Freeze the exact direct research ground.",
) -> ContextRevisionRecord:
    mission_ref = deep_thaw(authority.mission_root)
    return issue_context_revision(
        authority=authority,
        context_id=CONTEXT_ID,
        purpose=purpose,
        question="Which exact source constrains the current research decision?",
        indispensable_ground=(
            {
                "reference": mission_ref,
                "why": "it is the current Mission semantic root",
            },
        ),
        owner_source_references=(
            {
                "reference": mission_ref,
                "retrieval": "direct current Mission owner revision",
                "provenance": "active Executive Epoch Mission root",
            },
        ),
        known_omissions=("unrelated proof programs",),
        restricted_uses=("do not infer RH from operational completion",),
        independence_treatment={"method": "direct source comparison"},
        restrictions=("raw output has no mathematical authority",),
        invalidation_conditions=(
            {
                "reference": mission_ref,
                "condition": "the current Mission root changes",
            },
        ),
    )


def _stored(
    record: ContextRevisionRecord,
    *,
    revision: int,
) -> Mapping[str, Any]:
    return {
        "context_id": CONTEXT_ID,
        "revision": revision,
        "payload": deep_thaw(record.document),
        "payload_digest": record.digest_sha256,
        "predecessor_revision": None if revision == 1 else revision - 1,
        "created_actor": "coordinating-codex",
        "created_at": f"context-revision-{revision}",
    }


def _exact_reference(
    identity: str,
    *,
    revision: int = 1,
    kind: str = "evidence",
) -> dict[str, Any]:
    return {
        "kind": kind,
        "identity": identity,
        "revision": revision,
        "payload_sha256": _digest((kind, identity, revision)),
    }


def _scientific_source(
    reference: Mapping[str, Any],
    *,
    roles: tuple[str, ...] = ("reliance",),
    selection: Mapping[str, Any] | None = None,
    observed_current: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "reference": deep_thaw(reference),
        "selection": None if selection is None else deep_thaw(selection),
        "roles": list(roles),
        "why": "The treatment uses this exact source in the stated role.",
        "dependency": (
            None
            if observed_current is None
            else {
                "observed_current_reference": deep_thaw(observed_current),
                "change_that_matters": "A qualification of the selected estimate changes.",
                "dependent_judgment": "Whether the selected estimate composes with the consumer.",
            }
        ),
    }


def _treatment(*sources: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "question": "Does the retained endpoint estimate compose with this consumer?",
        "account": "The selected estimate survives; combined reach remains unresolved.",
        "qualifications": [
            "Use the stated normalization, not the defeated saddle normalization.",
            "The estimate is uniform only on the stated compact domain.",
        ],
        "sources": [deep_thaw(source) for source in sources],
    }


def _scientific_record(
    authority: DirectExecutiveEpochAuthority,
    *,
    treatments: Mapping[str, Any],
    exposed_treatments: tuple[Mapping[str, Any], ...] = (),
    independence_treatment: Mapping[str, Any] | None = None,
) -> ContextRevisionRecord:
    return issue_context_revision_v3(
        authority=authority,
        context_id=CONTEXT_ID,
        purpose="Preserve the parent program and qualified surviving interfaces.",
        question="Which accumulated results can change the Mission's available constructions?",
        treatments=treatments,
        exposed_treatments=exposed_treatments,
        known_omissions=("unsearched external literature",),
        restricted_uses=("scientific understanding is not canonical Admission",),
        restrictions=("do not infer RH from operational completion",),
        independence_treatment=(
            {"method": "compare exact source qualifications"}
            if independence_treatment is None
            else independence_treatment
        ),
    )


def _scientific_patch(**changes: Any) -> dict[str, Any]:
    patch: dict[str, Any] = {
        "insert": {},
        "replace": {},
        "remove": [],
        "exposure_add": [],
        "exposure_remove": [],
        "metadata_replace": {},
    }
    patch.update(changes)
    return patch


def _scientific_update(
    record: ContextRevisionRecord,
    *,
    patch: Mapping[str, Any],
    revision: int = 1,
) -> dict[str, Any]:
    return {
        "context_id": f"context:{CONTEXT_ID}",
        "expected_head": {
            "kind": "context",
            "identity": CONTEXT_ID,
            "revision": revision,
            "payload_sha256": record.digest_sha256,
        },
        "create": None,
        "patch": deep_thaw(patch),
    }


class _ContextStore:
    def __init__(
        self,
        *,
        current_authority: DirectExecutiveEpochAuthority,
        history: tuple[ContextRevisionRecord, ...] = (),
    ) -> None:
        self.current_authority = current_authority
        self.history = history
        self.commit_kwargs: Mapping[str, Any] | None = None

    def commit_context_revision(self, **kwargs: Any) -> str:
        if (
            kwargs["executive_epoch_id"] != self.current_authority.executive_epoch_id
            or kwargs["expected_canonical_authority_digest"]
            != self.current_authority.canonical_authority_digest
        ):
            raise ValueError("Context commit authority is not current")
        self.commit_kwargs = kwargs
        return "committed"

    def read_context_revision(
        self,
        context_id: str,
        revision: int | None = None,
    ) -> Mapping[str, Any]:
        if context_id != CONTEXT_ID or not self.history:
            raise ValueError("Context revision is absent")
        selected_revision = len(self.history) if revision is None else revision
        return _stored(
            self.history[selected_revision - 1],
            revision=selected_revision,
        )


class DirectContextRevisionTests(unittest.TestCase):
    def test_v3_reference_roles_separate_retention_discovery_basis_and_guards(self) -> None:
        evidence_1 = _exact_reference("evidence.endpoint", revision=1)
        evidence_2 = _exact_reference("evidence.endpoint", revision=2)
        recognition = _exact_reference("evidence.recognized")
        background = _exact_reference("evidence.background")
        excluded_by_independence = _exact_reference("evidence.excluded-from-independence")
        record = _scientific_record(
            _authority(),
            treatments={
                "bridge": _treatment(
                    _scientific_source(evidence_1, observed_current=evidence_2),
                    _scientific_source(recognition, roles=("recognition",)),
                    _scientific_source(background, roles=("history",)),
                ),
            },
            independence_treatment={
                "method": "derive without the excluded reference",
                "excluded_reference": excluded_by_independence,
            },
        )
        expected = {
            "retention": (
                evidence_1, evidence_2, recognition, background, excluded_by_independence,
            ),
            "discovery": (evidence_1, recognition, background),
            "mathematical_basis": (evidence_1,),
            "concurrency": (evidence_2,),
        }
        for purpose, references in expected.items():
            with self.subTest(purpose=purpose):
                self.assertCountEqual(
                    deep_thaw(
                        context_reference_projection(
                            record.document,
                            purpose=purpose,
                            treatment_ids=("bridge",),
                        )
                    ),
                    [{"reference": reference, "selection": None} for reference in references],
                )
        self.assertEqual(record.dependency_heads, ())
        with self.assertRaisesRegex(ValueError, "explicit treatment selection"):
            context_reference_projection(record.document, purpose="mathematical_basis")

    def test_v3_genuine_historical_reliance_remains_an_exact_basis_member(self) -> None:
        historical = _exact_reference("evidence.old-lemma", revision=1)
        record = _scientific_record(
            _authority(),
            treatments={
                "bridge": _treatment(
                    _scientific_source(historical, roles=("history", "reliance")),
                ),
            },
        )
        self.assertEqual(
            deep_thaw(
                context_reference_projection(
                    record.document,
                    purpose="mathematical_basis",
                    treatment_ids=("bridge",),
                )
            ),
            [{"reference": historical, "selection": None}],
        )
        self.assertEqual(
            context_reference_projection(record.document, purpose="concurrency"),
            (),
        )

    def test_v3_context_source_keeps_exact_revision_and_selected_treatment(self) -> None:
        selected_context = _exact_reference("context.endpoint", kind="context", revision=4)
        selection = {"mode": "treatments", "treatment_ids": ["uniform-endpoint"]}
        unrelated = _exact_reference("evidence.unselected-construction")
        record = _scientific_record(
            _authority(),
            treatments={
                "bridge": _treatment(
                    _scientific_source(selected_context, selection=selection),
                ),
                "other-construction": _treatment(_scientific_source(unrelated)),
            },
        )
        expected = {"reference": selected_context, "selection": selection}
        for purpose in ("retention", "discovery", "mathematical_basis"):
            with self.subTest(purpose=purpose):
                self.assertEqual(
                    deep_thaw(
                        context_reference_projection(
                            record.document,
                            purpose=purpose,
                            treatment_ids=("bridge",),
                        )
                    ),
                    [expected],
                )
        self.assertCountEqual(
            deep_thaw(
                context_reference_projection(
                    record.document,
                    purpose="mathematical_basis",
                    whole_context=True,
                )
            ),
            [expected, {"reference": unrelated, "selection": None}],
        )

    def test_v3_whole_context_source_use_is_explicit_and_not_narrowed(self) -> None:
        source = _exact_reference("context.whole-source", kind="context", revision=3)
        record = _scientific_record(
            _authority(),
            treatments={
                "bridge": _treatment(
                    _scientific_source(source, selection={"mode": "whole_context"}),
                ),
            },
        )
        self.assertEqual(
            deep_thaw(
                context_reference_projection(
                    record.document,
                    purpose="mathematical_basis",
                    treatment_ids=("bridge",),
                )
            ),
            [{"reference": source, "selection": {"mode": "whole_context"}}],
        )

    def test_v3_patch_preserves_qualified_A_and_only_guards_changed_B(self) -> None:
        authority = _authority()
        old_source = _exact_reference("evidence.A", revision=1)
        observed_a = _exact_reference("evidence.A", revision=2)
        observed_b = _exact_reference("evidence.B", revision=3)
        treatment_a = _treatment(_scientific_source(old_source, observed_current=observed_a))
        record = _scientific_record(
            authority,
            treatments={"A": treatment_a, "B": _treatment()},
            exposed_treatments=(
                {"context_id": "context.deeper", "treatment_id": "retained-interface"},
            ),
        )
        previous = read_context_revision(
            _ContextStore(current_authority=authority, history=(record,)),
            context_id=CONTEXT_ID,
        )
        replacement_b = _treatment(
            _scientific_source(observed_b, observed_current=observed_b),
        )
        replacement_b["account"] = (
            "The new construction removes only the stated width obstruction."
        )
        prepared = prepare_scientific_context_update(
            authority=authority,
            previous=previous,
            update=_scientific_update(
                record, patch=_scientific_patch(replace={"B": replacement_b}),
            ),
        )
        self.assertTrue(prepared.changed)
        self.assertEqual(prepared.changed_treatment_ids, ("B",))
        self.assertEqual(
            prepared.dependency_heads,
            {"evidence:evidence.B": (3, observed_b["payload_sha256"])},
        )
        self.assertEqual(prepared.record.dependency_heads, ())
        self.assertEqual(
            canonical_json_bytes(prepared.record.document["treatments"]["A"]),
            canonical_json_bytes(treatment_a),
        )
        self.assertEqual(
            prepared.record.document["exposed_treatments"],
            record.document["exposed_treatments"],
        )
        self.assertEqual(prepared.record.document["question"], record.document["question"])
        self.assertEqual(deep_thaw(prepared.record.document["treatments"]["B"]), replacement_b)

    def test_v3_semantic_noop_has_no_changed_treatment_or_dependency_guard(self) -> None:
        authority = _authority()
        source = _exact_reference("evidence.endpoint", revision=1)
        current = _exact_reference("evidence.endpoint", revision=2)
        treatment = _treatment(_scientific_source(source, observed_current=current))
        record = _scientific_record(authority, treatments={"bridge": treatment})
        previous = read_context_revision(
            _ContextStore(current_authority=authority, history=(record,)),
            context_id=CONTEXT_ID,
        )
        prepared = prepare_scientific_context_update(
            authority=authority,
            previous=previous,
            update=_scientific_update(
                record,
                patch=_scientific_patch(replace={"bridge": treatment}),
            ),
        )
        self.assertFalse(prepared.changed)
        self.assertEqual(prepared.record.digest_sha256, record.digest_sha256)
        self.assertEqual(prepared.changed_treatment_ids, ())
        self.assertEqual(prepared.dependency_heads, {})

    def test_v3_patch_rejects_wrong_expected_head_without_rebasing(self) -> None:
        authority = _authority()
        record = _scientific_record(authority, treatments={"bridge": _treatment()})
        previous = read_context_revision(
            _ContextStore(current_authority=authority, history=(record,)),
            context_id=CONTEXT_ID,
        )
        for field, wrong_value in (
            ("kind", "evidence"),
            ("identity", "context.somewhere-else"),
            ("revision", 2),
            ("payload_sha256", "0" * 64),
        ):
            with self.subTest(field=field):
                update = _scientific_update(record, patch=_scientific_patch())
                update["expected_head"][field] = wrong_value
                with self.assertRaises(ValueError):
                    prepare_scientific_context_update(
                        authority=authority,
                        previous=previous,
                        update=update,
                    )

    def test_v3_rejects_invalid_source_scope_and_current_head_substitution(self) -> None:
        context_source = _exact_reference("context.source", kind="context")
        evidence = _exact_reference("evidence.source")
        invalid_sources = (
            _scientific_source(context_source),
            _scientific_source(context_source, selection={"mode": "current"}),
            _scientific_source(
                context_source,
                selection={"mode": "treatments", "treatment_ids": []},
            ),
            _scientific_source(
                context_source,
                selection={"mode": "treatments", "treatment_ids": ["bridge", "bridge"]},
            ),
            _scientific_source(evidence, selection={"mode": "whole_context"}),
            _scientific_source(
                evidence,
                roles=("recognition",),
                observed_current=evidence,
            ),
            _scientific_source(
                evidence,
                observed_current=_exact_reference("evidence.different"),
            ),
        )
        for index, source in enumerate(invalid_sources):
            with self.subTest(case=index):
                with self.assertRaises(ValueError):
                    _scientific_record(
                        _authority(),
                        treatments={"bridge": _treatment(source)},
                    )

    def test_v3_rejects_self_exposure_and_missing_selected_treatment(self) -> None:
        authority = _authority()
        with self.assertRaises(ValueError):
            _scientific_record(
                authority,
                treatments={"bridge": _treatment()},
                exposed_treatments=({"context_id": CONTEXT_ID, "treatment_id": "bridge"},),
            )
        record = _scientific_record(authority, treatments={"bridge": _treatment()})
        with self.assertRaises(ValueError):
            context_reference_projection(
                record.document,
                purpose="mathematical_basis",
                treatment_ids=("missing",),
            )

    def test_v3_creation_resolves_this_evidence_without_persisting_symbol(self) -> None:
        authority = _authority()
        evidence = _exact_reference("evidence.paired", revision=3)
        source = _scientific_source(evidence)
        create = deep_thaw(
            _scientific_record(
                authority, treatments={"unfinished-bridge": _treatment(source)},
            ).document
        )
        create["treatments"]["unfinished-bridge"]["sources"][0]["reference"] = {
            "source": "this_evidence",
        }
        update = {
            "context_id": f"context:{CONTEXT_ID}",
            "expected_head": None,
            "create": create,
            "patch": None,
        }
        with self.assertRaises(ValueError):
            prepare_scientific_context_update(
                authority=authority,
                previous=None,
                update=update,
            )
        prepared = prepare_scientific_context_update(
            authority=authority,
            previous=None,
            update=update,
            this_evidence_reference=evidence,
        )
        self.assertTrue(prepared.changed)
        self.assertIsNone(prepared.expected_head)
        self.assertEqual(prepared.changed_treatment_ids, ("unfinished-bridge",))
        self.assertEqual(
            prepared.record.document["treatments"]["unfinished-bridge"]["sources"][0]["reference"],
            evidence,
        )
        self.assertNotIn(b"this_evidence", canonical_json_bytes(prepared.record.document))
        self.assertEqual(
            update["create"]["treatments"]["unfinished-bridge"]["sources"][0]["reference"],
            {"source": "this_evidence"},
        )

    def test_v3_create_and_patch_preserve_ordered_survivors_and_unmentioned_metadata(self) -> None:
        authority = _authority()
        removed_source = _exact_reference("evidence.removed", revision=7)
        inserted_source = _exact_reference("evidence.new", revision=2)
        treatment_a = _treatment()
        treatment_b = _treatment(
            _scientific_source(removed_source, observed_current=removed_source),
        )
        first = {"context_id": "context.z", "treatment_id": "first"}
        removed = {"context_id": "context.b", "treatment_id": "removed"}
        last = {"context_id": "context.a", "treatment_id": "last"}
        added = {"context_id": "context.c", "treatment_id": "added"}
        create = deep_thaw(
            _scientific_record(authority, treatments={"A": treatment_a, "B": treatment_b}).document
        )
        # Valid persisted membership need not have the issuer's canonical order.
        create["exposed_treatments"] = [first, removed, last]
        created = prepare_scientific_context_update(
            authority=authority,
            previous=None,
            update={
                "context_id": f"context:{CONTEXT_ID}",
                "expected_head": None,
                "create": create,
                "patch": None,
            },
        )
        self.assertEqual(deep_thaw(created.record.document), create)
        self.assertEqual(created.changed_treatment_ids, ("A", "B"))
        previous = read_context_revision(
            _ContextStore(current_authority=authority, history=(created.record,)),
            context_id=CONTEXT_ID,
        )
        treatment_c = _treatment(
            _scientific_source(inserted_source, observed_current=inserted_source),
        )
        question = "Which surviving interface admits the new construction?"
        prepared = prepare_scientific_context_update(
            authority=authority,
            previous=previous,
            update=_scientific_update(
                created.record,
                patch=_scientific_patch(
                    insert={"C": treatment_c},
                    remove=[{"treatment_id": "B", "reason": "Replace the defeated instance."}],
                    exposure_add=[added],
                    exposure_remove=[{**removed, "reason": "The selected instance was removed."}],
                    metadata_replace={"question": question, "known_omissions": []},
                ),
            ),
        )
        expected = {
            **create,
            "treatments": {"A": treatment_a, "C": treatment_c},
            "exposed_treatments": [first, last, added],
            "question": question,
            "known_omissions": [],
        }
        self.assertTrue(prepared.changed)
        self.assertEqual(deep_thaw(prepared.record.document), expected)
        self.assertEqual(prepared.changed_treatment_ids, ("C",))
        self.assertEqual(
            prepared.dependency_heads,
            {"evidence:evidence.new": (2, inserted_source["payload_sha256"])},
        )
        self.assertEqual(deep_thaw(previous.record.document), create)

    def test_v3_rejects_ambiguous_edits_scope_changes_and_legacy_write_routes(self) -> None:
        authority = _authority()
        treatment = _treatment()
        membership = {"context_id": "context.deeper", "treatment_id": "bridge"}
        new_membership = {"context_id": "context.new", "treatment_id": "bridge"}
        removal = {"treatment_id": "A", "reason": "Scoped replacement."}
        record = _scientific_record(
            authority,
            treatments={"A": treatment},
            exposed_treatments=(membership,),
        )
        store = _ContextStore(current_authority=authority, history=(record,))
        previous = read_context_revision(store, context_id=CONTEXT_ID)
        invalid_patches = {
            "insert-replace": _scientific_patch(insert={"B": treatment}, replace={"B": treatment}),
            "replace-remove": _scientific_patch(replace={"A": treatment}, remove=[removal]),
            "repeated-removal": _scientific_patch(remove=[removal, removal]),
            "insert-existing": _scientific_patch(insert={"A": treatment}),
            "replace-absent": _scientific_patch(replace={"missing": treatment}),
            "remove-absent": _scientific_patch(
                remove=[{"treatment_id": "missing", "reason": "Not actually present."}],
            ),
            "repeated-exposure-add": _scientific_patch(
                exposure_add=[new_membership, new_membership],
            ),
            "repeated-exposure-remove": _scientific_patch(
                exposure_remove=[{**membership, "reason": "Remove."}] * 2,
            ),
            "conflicting-exposure": _scientific_patch(
                exposure_add=[new_membership],
                exposure_remove=[{**new_membership, "reason": "Also remove."}],
            ),
            "add-existing-exposure": _scientific_patch(exposure_add=[membership]),
            "remove-absent-exposure": _scientific_patch(
                exposure_remove=[{**new_membership, "reason": "Not actually present."}],
            ),
            "patch-identity": _scientific_patch(metadata_replace={"context_id": "context.other"}),
        }
        for case, patch in invalid_patches.items():
            with self.subTest(case=case):
                with self.assertRaises(ValueError):
                    prepare_scientific_context_update(
                        authority=authority,
                        previous=previous,
                        update=_scientific_update(record, patch=patch),
                    )
        legacy = _record(authority)
        legacy_previous = read_context_revision(
            _ContextStore(current_authority=authority, history=(legacy,)),
            context_id=CONTEXT_ID,
        )
        with self.assertRaisesRegex(ValueError, "cannot implicitly convert legacy Context"):
            prepare_scientific_context_update(
                authority=authority,
                previous=legacy_previous,
                update=_scientific_update(legacy, patch=_scientific_patch()),
            )
        foreign_create = deep_thaw(record.document)
        foreign_create["mission_id"] = "mission.different"
        with self.assertRaisesRegex(ValueError, "another identity or Mission"):
            prepare_scientific_context_update(
                authority=authority,
                previous=None,
                update={
                    "context_id": f"context:{CONTEXT_ID}",
                    "expected_head": None,
                    "create": foreign_create,
                    "patch": None,
                },
            )
        with self.assertRaises(ValueError):
            commit_context_revision(
                store,
                authority=authority,
                record=record,
                lease=object(),
                actor="coordinating-codex",
            )
        self.assertIsNone(store.commit_kwargs)

    def test_v2_retains_advisory_history_without_new_mutable_head_guards(self) -> None:
        authority = _authority()
        mission_ref = deep_thaw(authority.mission_root)
        historical_ref = {
            "kind": "branch",
            "identity": "branch.retained",
            "revision": 2,
            "payload_sha256": "2" * 64,
        }
        record = issue_context_revision_v2(
            authority=authority,
            context_id=CONTEXT_ID,
            purpose="Search retained work for an overlooked bridge.",
            question="Can retained branch residue discharge the current premise?",
            indispensable_ground=(
                {"reference": mission_ref, "why": "current Mission root"},
            ),
            owner_source_references=(
                {
                    "reference": mission_ref,
                    "retrieval": "direct current Mission owner",
                    "provenance": "active Executive Epoch",
                },
            ),
            known_omissions=("external literature",),
            restricted_uses=("advisory only",),
            independence_treatment={"method": "independent retained-history search"},
            restrictions=("does not change Strategy",),
            invalidation_conditions=(),
            immutable_historical_references=(
                {
                    "reference": historical_ref,
                    "why": "the retained estimate may compose with the current route",
                },
            ),
            untrusted_material_locators=(
                {
                    "id": "capture:raw-capture:" + ("a" * 48),
                    "why": "inspect the exact observation if needed",
                    "treatment": "untrusted_mathematical_material",
                },
            ),
            historical_advisory_scope={
                "assignment_mode": "historical_opportunity_scout",
                "search_lens": "Find an exact retained estimate for the premise.",
                "source_families": ("branches", "strategies"),
                "known_omissions": ("external literature",),
                "coverage_limits": ("retained Mission history only",),
            },
        )

        self.assertEqual(record.document["schema_version"], 2)
        self.assertEqual(
            record.document["immutable_historical_references"][0]["reference"],
            historical_ref,
        )
        self.assertEqual(record.dependency_heads, (mission_ref,))

        store = _ContextStore(current_authority=authority, history=(record,))
        self.assertEqual(
            commit_context_revision(
                store,
                authority=authority,
                record=record,
                lease=object(),
                actor="coordinating-codex",
            ),
            "committed",
        )
        assert store.commit_kwargs is not None
        self.assertEqual(
            store.commit_kwargs["dependency_heads"],
            {f"mission:{MISSION_ID}": (1, "1" * 64)},
        )
        self.assertEqual(
            read_context_revision(store, context_id=CONTEXT_ID).record.document,
            record.document,
        )

    def test_record_rejects_content_digest_and_dependency_head_tampering(self) -> None:
        record = _record(_authority())
        self.assertEqual(
            set(record.__dataclass_fields__),
            {"document", "digest_sha256", "dependency_heads"},
        )

        changed_document = deep_thaw(record.document)
        changed_document["question"] = "A different question."
        with self.assertRaisesRegex(ValueError, "digest is stale"):
            replace(record, document=changed_document)
        with self.assertRaisesRegex(ValueError, "digest is stale"):
            replace(record, digest_sha256="0" * 64)
        with self.assertRaisesRegex(ValueError, "dependency heads are stale"):
            replace(record, dependency_heads=())

        open_document = deep_thaw(record.document)
        open_document["receipt"] = "forbidden"
        with self.assertRaisesRegex(ValueError, "wrong closed shape"):
            ContextRevisionRecord(
                document=open_document,
                digest_sha256=_digest(open_document),
                dependency_heads=record.dependency_heads,
            )

    def test_issue_and_commit_require_exact_direct_current_authority(self) -> None:
        authority = _authority()
        record = _record(authority)
        store = _ContextStore(current_authority=authority)

        mission_ref = deep_thaw(authority.mission_root)
        with self.assertRaisesRegex(TypeError, "direct Executive Epoch authority"):
            issue_context_revision(
                authority=object(),  # type: ignore[arg-type]
                context_id=CONTEXT_ID,
                purpose="Exact Context meaning.",
                question="Which source constrains this decision?",
                indispensable_ground=(
                    {"reference": mission_ref, "why": "current Mission root"},
                ),
                owner_source_references=(
                    {
                        "reference": mission_ref,
                        "retrieval": "direct owner read",
                        "provenance": "current Mission root",
                    },
                ),
                known_omissions=(),
                restricted_uses=(),
                independence_treatment={"method": "direct"},
                restrictions=(),
                invalidation_conditions=(),
            )
        with self.assertRaisesRegex(TypeError, "direct Executive Epoch authority"):
            commit_context_revision(
                store,
                authority=object(),  # type: ignore[arg-type]
                record=record,
                lease=object(),
                actor="coordinating-codex",
            )

        result = commit_context_revision(
            store,
            authority=authority,
            record=record,
            lease=object(),
            actor="coordinating-codex",
        )
        self.assertEqual(result, "committed")
        assert store.commit_kwargs is not None
        self.assertEqual(
            store.commit_kwargs["executive_epoch_id"],
            authority.executive_epoch_id,
        )
        self.assertEqual(
            store.commit_kwargs["expected_canonical_authority_digest"],
            authority.canonical_authority_digest,
        )

        stale_authority = _authority(epoch_id="epoch.context.stale")
        with self.assertRaisesRegex(ValueError, "not current"):
            commit_context_revision(
                store,
                authority=stale_authority,
                record=record,
                lease=object(),
                actor="coordinating-codex",
            )

    def test_historical_read_reconstructs_and_marks_noncurrent_revision(self) -> None:
        authority = _authority()
        first = _record(authority, purpose="First exact Context meaning.")
        second = _record(authority, purpose="Revised exact Context meaning.")
        store = _ContextStore(
            current_authority=authority,
            history=(first, second),
        )

        historical = read_context_revision(
            store,
            context_id=CONTEXT_ID,
            revision=1,
        )
        current = read_context_revision(store, context_id=CONTEXT_ID)

        self.assertEqual(historical.record.document, first.document)
        self.assertEqual(historical.payload_digest, first.digest_sha256)
        self.assertFalse(historical.is_current_head)
        self.assertEqual(current.record.document, second.document)
        self.assertTrue(current.is_current_head)

    def test_direct_imports_do_not_load_aggregate_context_or_campaign(self) -> None:
        script = """
import importlib
import sys
from pathlib import Path

package_root = Path(sys.argv[1]).resolve(strict=True)
sys.path.insert(0, str(package_root))

for module in ("mission_interface", "formal_session"):
    importlib.import_module(f"research_core.{module}")

for forbidden in (
    "context_catalog",
    "context_projection",
    "research_projections",
    "campaign_contract",
    "campaign_validator",
    "campaign_discovery_validator",
    "campaign_compatibility",
):
    qualified = f"research_core.{forbidden}"
    if qualified in sys.modules:
        raise AssertionError(f"unexpected transitive import: {qualified}")
"""
        completed = subprocess.run(
            [sys.executable, "-P", "-c", script, str(PACKAGE_ROOT)],
            cwd=PACKAGE_ROOT,
            check=False,
            capture_output=True,
            text=True,
        )
        self.assertEqual(
            completed.returncode,
            0,
            completed.stdout + completed.stderr,
        )


if __name__ == "__main__":
    unittest.main()
