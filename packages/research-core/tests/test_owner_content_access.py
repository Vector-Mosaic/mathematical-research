from __future__ import annotations

import hashlib
import json
import sqlite3
import sys
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch


PACKAGE_ROOT = Path(__file__).resolve().parents[1]
if str(PACKAGE_ROOT) not in sys.path:
    sys.path.insert(0, str(PACKAGE_ROOT))

from research_core import owner_content_access as access  # noqa: E402
from research_core import owner_content_index as index  # noqa: E402
from research_core.json_support import canonical_json_bytes  # noqa: E402
from research_core.owner_content_index import OwnerContentIndex  # noqa: E402
from research_core.owner_content_projection import (  # noqa: E402
    DELEGATED_FAMILY_KINDS, PROFILE_DIGEST, PROJECTION_FIELDS, ROOT_KINDS,
)
from research_core.workspace_store import WorkspaceIntegrityError  # noqa: E402


PROJECT = "project.rh"
MISSION = "mission.access-fixture"


class OwnerContentAccessTests(unittest.TestCase):
    """Private Store-adapter tests, not custody, migration, or live-cut proof.

    Sources are explicitly supplied fixtures. The real content-addressed index
    and adapter are exercised, while exact source authentication is represented
    by a callback whose arguments and returned projections are checked here.
    """

    def setUp(self):
        self.connection = sqlite3.connect(":memory:")
        self.addCleanup(self.connection.close)
        self.connection.execute(
            "CREATE TABLE owner_content_index_node ("
            "node_digest TEXT PRIMARY KEY, node_kind TEXT, node_json TEXT)"
        )

    def _source(
        self, identity="evidence.first", *, kind="evidence", revision=1,
        origin=1, content="qualified lemma", title="", variants=None,
        document=None,
    ):
        if variants is None:
            variants = tuple(v for v in PROJECTION_FIELDS if kind in PROJECTION_FIELDS[v])
        fields = {
            variant: {"id": identity, "title": title, "content": content, "relationships": ""}
            for variant in variants
        }
        digest = hashlib.sha256(f"{kind}:{identity}@{revision}:{content}".encode()).hexdigest()
        return access.SourceProjection(
            PROJECT, MISSION, kind, identity, revision, digest, origin,
            {} if document is None else document, fields,
        )

    def _bootstrap(self, retained, current=None, *, cut=100):
        retained = tuple(retained)
        current = retained if current is None else tuple(current)
        self.connection.execute("BEGIN")
        try:
            descriptor = access.bootstrap_owner_content_index(
                self.connection, project_id=PROJECT, source_project_commit=cut,
                retained_sources=retained, current_sources=current,
            )
            self.connection.commit()
            return descriptor
        except BaseException:
            self.connection.rollback()
            raise

    def _delta(self, descriptor, delta):
        self.connection.execute("BEGIN")
        try:
            updated = access.apply_owner_content_delta(
                self.connection, descriptor=descriptor, delta=delta,
            )
            self.connection.commit()
            return updated
        except BaseException:
            self.connection.rollback()
            raise

    def _query(self, descriptor, sources=(), *, calls=None, **options):
        sources = tuple(sources)
        variant = {"root_v1": "root", "delegated_v1": "delegated"}.get(
            options.get("projection_variant"), options.get("projection_variant", "root"),
        )

        def authenticated(item):
            if calls is not None:
                calls.append(item)
            matches = [source for source in sources
                       if source.reference == item["reference"]
                       and source.origin_commit == item["projection_commit"]
                       and variant in source.fields]
            self.assertEqual(len(matches), 1, item)
            return matches[0]

        arguments = {
            "project_id": PROJECT, "mission_id": MISSION,
            "descriptor": descriptor, "projection_variant": "root",
            "kinds": ("evidence",), "fields": ("content",),
            "query": "lemma", "source_project_commit": 100,
            "revision_scope": "current_at_cut", "authenticated_source": authenticated,
        }
        arguments.update(options)
        return access.query_index_candidates(self.connection, **arguments)

    def _all_pages(self, descriptor, sources, *, page_size=2, **options):
        items = []
        after = None
        while True:
            page = self._query(descriptor, sources, after=after, page_size=page_size, **options)
            items.extend(page["items"])
            if not page["has_more"]:
                self.assertIsNone(page["next_after"])
                return items
            self.assertEqual(len(page["items"]), page_size)
            self.assertNotEqual(page["next_after"], after)
            after = page["next_after"]

    @staticmethod
    def _capture(*, pending=True):
        capture = {
            "capture_id": "capture.fixture", "project_id": PROJECT,
            "mission_id": MISSION, "executive_epoch_id": "epoch.one",
            "capture_kind": "output", "observation_id": "observation.one",
            "assignment_id": "assignment.one", "provenance": {},
            "completion": {"state": "completed"},
            "artifacts": [{"ordinal": 0, "role": "output", "logical_name": "output.txt", "blob_sha256": "a" * 64}],
        }
        descriptor = {
            "id": "capture:capture.fixture", "kind": "capture", "title": "capture.fixture",
            "capture_kind": "output", "origin_epoch_id": "epoch.one",
            "origin_epoch_state": "completed", "late_classification": None,
            "cut_relation": "after_latest_checkpoint",
            "pending_state": "current_pending" if pending else "fully_covered",
            "native_lineage": None,
            "artifacts": [{"handle": "capture-artifact:capture.fixture#0", "ordinal": 0,
                           "role": "output", "logical_name": "output.txt", "pending": pending}],
            "failed_interval_or_late_output": False,
            "trust_class": "custody_descriptor", "completeness": "descriptor",
        }
        return capture, descriptor

    def test_source_projection_requires_closed_supported_variants_and_fields(self):
        # Literal codepoint expectations, not another call to the derivation
        # helper: casefold expands both sharp-s and capital dotted-I and does
        # not normalize away the following combining acute accent.
        self.assertEqual(list(index._field_gram_positions({
            "title": "İ\u0301", "id": "", "content": "Straße",
        })), [
            ("content", 7, {
                "s": [0, 4, 5], "t": [1], "r": [2], "a": [3], "e": [6],
                "st": [0], "tr": [1], "ra": [2], "as": [3], "ss": [4], "se": [5],
                "str": [0], "tra": [1], "ras": [2], "ass": [3], "sse": [4],
            }),
            ("id", 0, {}),
            ("title", 3, {"i": [0], "\u0307": [1], "\u0301": [2],
                           "i\u0307": [0], "\u0307\u0301": [1], "i\u0307\u0301": [0]}),
        ])
        self.assertEqual(list(index._field_gram_positions({})), [])
        for value in (None, 1, True, b"text"):
            with self.subTest(non_string=value), self.assertRaisesRegex(ValueError, "supply strings"):
                list(index._field_gram_positions({"content": value}))
        source = self._source()
        self.assertEqual(set(source.fields), {"root", "delegated"})
        self.assertEqual(source.handle, "evidence:evidence.first@1")
        self.assertEqual(source.reference, {
            "kind": "evidence", "identity": "evidence.first", "revision": 1,
            "payload_sha256": source.payload_digest,
        })
        for fields in (
            {}, {"unknown": source.fields["root"]},
            {"root": {"content": "partial"}},
            {"root": {**source.fields["root"], "body": "not authorized"}},
            {"root": {**source.fields["root"], "title": None}},
        ):
            with self.subTest(fields=fields), self.assertRaises(ValueError):
                replace(source, fields=fields)
        with self.assertRaisesRegex(ValueError, "unsupported projection"):
            self._source(kind="mission", variants=("delegated",))
        with self.assertRaisesRegex(ValueError, "source origin"):
            replace(source, source_origin_commit=source.origin_commit + 1)
        for derive in (access.source_entries, access.source_descriptor_entries):
            with self.subTest(derive=derive.__name__), self.assertRaisesRegex(ValueError, "revision scope"):
                list(derive(source, "unknown"))

    def test_strategy_projection_preserves_predecessor_sources_and_modern_selectors(self):
        from research_core.mission_frontier import _strategy_record
        from research_core.mission_interface import _readable_research_value
        from test_mission_frontier import _neutral_strategy_payload

        # Synthetic legacy-shape record exercises exact source preservation.
        capsule = {
            "strategy_id": "strategy.rh.public.1", "revision": 1,
            "mission_id": "mission.rh.public.1", "branch_id": "branch.rh.public.1",
            "is_current": True, "lifecycle": "active", "branch_revision": 1,
            "attention_state": "active",
            "decision_capsule": {
                "decision_class": "ordinary_continuation", "attention_transition": "continue",
                "selected_action": "Run a productive autonomous executive epoch against the current RH frontier.",
                "selected_ready_unit": {
                    "unit_kind": "source_work",
                    "exact_reference": "projects/riemann_hypothesis/research_state.json#project.active_target",
                    "question": "What is the highest-leverage unresolved mathematical question on the current RH frontier, and which investigation would most change the next decision?",
                    "decisive_discriminator": "The work produces a candidate lemma or derivation, resolves an open obligation, weakens or closes a route, or identifies a precise blocker and next experiment.",
                    "expected_decision_consequence": "Advance, redirect, or retire the route according to the mathematical result and its limitations.",
                    "stop_or_reconsideration_condition": "Change method or stop when the work only repackages known material or cannot identify a credible next mathematical experiment.",
                    "output_contract_sha256": "a" * 64,
                    "resolution_requirement_ref": None,
                },
                "exact_no_unit_reason": None,
                "concise_rationale": "Prioritize mathematical frontier change and use independent workers only when their distinct work can answer the current question.",
                "evidence_basis": [], "eligibility_frontier": [], "successor_mapping": None,
            },
            "decision_capsule_sha256": "b" * 64,
        }
        # A headless historical revision also preserves its exact source bytes.
        placeholder = {"strategy_id": "strategy.rh.public.1", "revision": 2,
                       "mission_id": "mission.rh.public.1", "placeholder": True}

        def project(document, *, revision, origin, retired=None):
            return access.owner_source_projection(
                project_id=PROJECT, kind="strategy", identity=document["strategy_id"],
                revision=revision, payload_digest=hashlib.sha256(canonical_json_bytes(document)).hexdigest(),
                origin_commit=origin, document=document, retired_at_commit=retired,
                row={"predecessor_revision": None if revision == 1 else revision - 1,
                     "created_actor": "fixture", "created_at": "2026-08-20T13:12:56Z"},
            )

        sources = []
        for document, origin in ((capsule, 2), (placeholder, 4)):
            with self.subTest(revision=document["revision"]):
                with self.assertRaisesRegex(ValueError, "wrong closed shape"):
                    _strategy_record(document)
                frozen_bytes = canonical_json_bytes(document)
                source = project(document, revision=document["revision"], origin=origin, retired=8)
                self.assertEqual(canonical_json_bytes(source.document), frozen_bytes)
                self.assertEqual(source.reference, {
                    "kind": "strategy", "identity": document["strategy_id"],
                    "revision": document["revision"],
                    "payload_sha256": hashlib.sha256(frozen_bytes).hexdigest(),
                })
                self.assertEqual(source.origin_commit, origin)
                self.assertEqual(source.selected_at_commit, origin)
                self.assertEqual(source.retired_at_commit, 8)
                self.assertEqual(json.loads(source.fields["delegated"]["content"]), {
                    **_readable_research_value(document), "formal_requests": [],
                })
                sources.append(source)

        # Exercise the actual migration index owner, not just a preprojected
        # SourceProjection fixture. These sources are retired, not current.
        descriptor = self._bootstrap(sources, current=())
        for source, query in zip(sources, ("productive autonomous executive epoch", "placeholder")):
            result = self._query(
                descriptor, sources, projection_variant="delegated",
                mission_id=capsule["mission_id"], kinds=("strategy",),
                query=query, revision_scope="retained_history",
            )
            self.assertEqual([item["reference"] for item in result["items"]], [source.reference])
            self.assertEqual(result["items"][0]["source_origin_project_commit"], source.origin_commit)
        self.assertEqual(self._query(
            descriptor, sources, mission_id=capsule["mission_id"],
            kinds=("strategy",), fields=(), query="",
        )["items"], [])

        modern = dict(_neutral_strategy_payload(mission_id=MISSION))
        bet = {"bet": "retained formal bet", "discriminator": "exact discriminator", "owner_refs": [],
               "formal_request": {"purpose": "targeted_verification", "context_ref": {
                   "kind": "context", "identity": "context.formal", "revision": 2,
                   "payload_sha256": "a" * 64,
               }}}
        modern["selected_bets"] = [bet]
        # A non-current modern revision must retain the same real owner-derived
        # selector, never be treated as predecessor provenance because of age.
        modern_source = project(modern, revision=3, origin=9, retired=12)
        self.assertEqual(json.loads(modern_source.fields["delegated"]["content"])["formal_requests"], [{
            "selected_bet_sha256": hashlib.sha256(canonical_json_bytes(bet)).hexdigest(),
            "bet": bet["bet"], "discriminator": bet["discriminator"],
            "purpose": "targeted_verification", "context_retrieval_handle": "context:context.formal@2",
        }])
        for malformed in (
            {key: value for key, value in modern.items() if key != "kind"},
            {**modern, "kind": "wrong_strategy"},
            {**capsule, "selected_bets": []},
            {**capsule, "schema_version": 1},
            {**placeholder, "placeholder": False},
            {**placeholder, "unrecognized": True},
        ):
            with self.subTest(malformed=malformed), self.assertRaises(ValueError):
                project(malformed, revision=malformed.get("revision", 3), origin=9)
        # Legacy complete-poststate writes advanced the typed row even when
        # the immutable semantic payload was unchanged. Its two revisions are
        # deliberately independent, unlike the exact identity binding.
        copied = project(capsule, revision=7, origin=9)
        self.assertEqual(copied.revision, 7)
        self.assertEqual(copied.document["revision"], 1)
        self.assertEqual(canonical_json_bytes(copied.document), canonical_json_bytes(capsule))
        for bad_revision in (0, -1, True, "1"):
            with self.subTest(bad_revision=bad_revision), self.assertRaises(ValueError):
                project({**capsule, "revision": bad_revision}, revision=7, origin=9)
        with self.assertRaises(ValueError):
            access.owner_source_projection(
                project_id=PROJECT, kind="strategy", identity="strategy.wrong",
                revision=7, payload_digest=hashlib.sha256(canonical_json_bytes(capsule)).hexdigest(),
                origin_commit=9, document=capsule,
                row={"predecessor_revision": 6, "created_actor": "fixture", "created_at": "fixture"},
            )

    def test_inventory_and_lexical_entries_cover_root_nine_and_delegated_eight(self):
        source = access.SourceProjection(
            PROJECT, MISSION, "context", "context.descriptor", 4, "a" * 64, 9,
            {"known_omissions": ["Only the qualified finite case is supported."]},
            {"delegated": {"id": "", "title": "", "content": "İ", "relationships": ""},
             "root": {"id": "", "title": "", "content": "ß", "relationships": ""}},
            source_origin_commit=3, selected_at_commit=10, retired_at_commit=12,
        )
        expected_value = {
            "reference": "context:context.descriptor@4", "payload_digest": "a" * 64,
            "origin_commit": 9, "source_origin_commit": 3, "positions": [], "field_length": None,
            "metadata": {"reference": {"kind": "context", "identity": "context.descriptor",
                "revision": 4, "payload_sha256": "a" * 64},
                "selected_at_commit": 10, "retired_at_commit": 12},
        }
        for scope in ("current_at_cut", "retained_history"):
            with self.subTest(exact_stream_scope=scope):
                expected, descriptors = [], []
                # Deliberately delegated first: preserve supplied variant
                # order, inventory-before-lexical, and lexical gram ordering.
                for variant, grams in (("delegated", (("i", [0]), ("i\u0307", [0]), ("\u0307", [1]))),
                                       ("root", (("s", [0, 1]), ("ss", [0])))):
                    inventory = (("owner_inventory", PROJECT, MISSION, scope, variant, "context"),
                                 ("context", "context.descriptor", 4), expected_value)
                    expected.append(inventory)
                    descriptors.append(inventory)
                    expected.extend((("lexical", PROJECT, MISSION, scope, variant, "context", "content", gram),
                        ("context", "context.descriptor", 4),
                        {**expected_value, "positions": positions, "field_length": 2})
                        for gram, positions in grams)
                if scope == "current_at_cut":
                    presence = (("current_field_presence", PROJECT, MISSION, "context", "known_omissions"),
                                ("context", "context.descriptor"), expected_value)
                    expected.append(presence)
                    descriptors.append(presence)
                self.assertEqual([(directory, posting.key, posting.to_mapping())
                                  for directory, posting in access.source_entries(source, scope)], expected)
                with patch.object(index, "_field_gram_positions", side_effect=AssertionError("lexical work")):
                    self.assertEqual([(directory, posting.key, posting.to_mapping())
                        for directory, posting in access.source_descriptor_entries(source, scope)], descriptors)
        self.assertEqual(set(ROOT_KINDS), {
            "mission", "strategy", "branch", "candidate", "context", "evidence",
            "session", "capture", "capture-annotation",
        })
        self.assertEqual(set(DELEGATED_FAMILY_KINDS.values()), {
            "strategy", "branch", "candidate", "context", "evidence",
            "capture", "capture-annotation", "capture-artifact",
        })
        capture, root = self._capture()
        capture["capture_id"] = "capture.unique-capture"
        capture["artifacts"][0]["logical_name"] = "unique-capture-artifact.txt"
        root["id"] = "capture:capture.unique-capture"
        root["title"] = "capture.unique-capture"
        root["artifacts"][0]["handle"] = "capture-artifact:capture.unique-capture#0"
        root["artifacts"][0]["logical_name"] = "unique-capture-artifact.txt"
        # Capture has separate immutable delegated and cut-dependent root
        # projections; the actual constructor also supplies its exact selectors.
        sources = tuple(self._source(
            identity=f"fixture.{kind}", kind=kind, content=f"unique-{kind}",
        ) for kind in sorted(set(ROOT_KINDS) | set(DELEGATED_FAMILY_KINDS.values()))
          if kind not in {"capture", "capture-artifact"}) + access.capture_source_projections(
            project_id=PROJECT, capture=capture, origin_commit=1,
            root_descriptor=root, projection_commit=1,
        )
        descriptor = self._bootstrap(sources)
        for variant, kinds in (("root_v1", ROOT_KINDS), ("delegated_v1", tuple(DELEGATED_FAMILY_KINDS.values()))):
            with self.subTest(variant=variant):
                inventory = self._all_pages(
                    descriptor, sources, projection_variant=variant,
                    kinds=kinds, fields=(), query="",
                )
                self.assertEqual({item["reference"]["kind"] for item in inventory}, set(kinds))
                self.assertEqual(len(inventory), len(kinds))
                for kind in kinds:
                    result = self._query(
                        descriptor, sources, projection_variant=variant,
                        kinds=kinds, query=f"unique-{kind}",
                    )
                    # Substring matching intentionally lets capture also match
                    # capture-annotation/artifact; the requested family must not disappear.
                    self.assertIn(kind, {item["reference"]["kind"] for item in result["items"]})

    def test_root_capture_occurrence_keeps_immutable_reference_and_source_origin(self):
        capture, root = self._capture()
        sources = access.capture_source_projections(
            project_id=PROJECT, capture=capture, origin_commit=3,
            root_descriptor=root, projection_commit=7,
        )
        delegated, root_source, artifact = sources
        self.assertEqual(set(delegated.fields), {"delegated"})
        self.assertEqual(set(root_source.fields), {"root"})
        self.assertEqual(set(artifact.fields), {"delegated"})
        self.assertEqual(root_source.reference, delegated.reference)
        self.assertEqual(root_source.handle, delegated.handle)
        self.assertEqual(root_source.reference["revision"], 0)
        self.assertEqual((delegated.origin_commit, root_source.origin_commit), (3, 7))
        self.assertEqual(root_source.source_origin_commit, 3)
        self.assertEqual(artifact.handle, "capture-artifact:capture.fixture#0")
        self.assertEqual(artifact.reference["revision"], 0)
        capture_digest = hashlib.sha256(canonical_json_bytes(capture)).hexdigest()
        for source, variant, revision, occurrence in ((delegated, "delegated", 0, 3),
                                                      (root_source, "root", 7, 7)):
            for scope in ("current_at_cut", "retained_history"):
                metadata = {"reference": {"kind": "capture", "identity": "capture.fixture",
                    "revision": 0, "payload_sha256": capture_digest},
                    "selected_at_commit": None, "retired_at_commit": None}
                if variant == "root":
                    metadata["capture_descriptor"] = root
                expected = [(("owner_inventory", PROJECT, MISSION, scope, variant, "capture"),
                    ("capture", "capture.fixture", revision), {
                        "reference": "capture:capture.fixture", "payload_digest": capture_digest,
                        "origin_commit": occurrence, "source_origin_commit": 3,
                        "positions": [], "field_length": None, "metadata": metadata,
                    })]
                if variant == "root" and scope == "current_at_cut":
                    expected.append((("capture_epoch_kind", PROJECT, MISSION,
                        "current_at_cut", "root", "capture", "epoch", "epoch.one", "output"),
                        ("capture", "capture.fixture", 7), {
                            "reference": "capture:capture.fixture", "payload_digest": capture_digest,
                            "origin_commit": 7, "source_origin_commit": 3,
                            "positions": [], "field_length": None,
                            "metadata": {"reference": {"kind": "capture", "identity": "capture.fixture",
                                "revision": 0, "payload_sha256": capture_digest},
                                "selected_at_commit": None, "retired_at_commit": None},
                        }))
                with self.subTest(descriptor_variant=variant, scope=scope):
                    with patch.object(index, "_field_gram_positions", side_effect=AssertionError("lexical work")):
                        self.assertEqual([(directory, posting.key, posting.to_mapping())
                            for directory, posting in access.source_descriptor_entries(source, scope)], expected)
                    ordinary = list(access.source_entries(source, scope))
                    self.assertEqual((ordinary[0][0], ordinary[0][1].key, ordinary[0][1].to_mapping()), expected[0])
                    lexical = ordinary[1:]
                    if len(expected) == 2:
                        self.assertEqual((ordinary[-1][0], ordinary[-1][1].key, ordinary[-1][1].to_mapping()), expected[1])
                        lexical = lexical[:-1]
                    self.assertTrue(lexical)
                    self.assertTrue(all(directory[0] == "lexical" for directory, _posting in lexical))
                    self.assertTrue(all("capture_descriptor" not in posting.metadata for _directory, posting in ordinary[1:]))
        for invalid in (
            replace(root_source, document={}),
            replace(root_source, inventory_metadata={}),
            replace(root_source, inventory_metadata={
                "capture_descriptor": {**root, "origin_epoch_id": "epoch.other"},
            }),
            replace(root_source, inventory_metadata={
                "capture_descriptor": {**root, "capture_kind": "assignment"},
            }),
        ):
            with self.subTest(invalid_capture=invalid), self.assertRaises(ValueError):
                list(access.source_descriptor_entries(invalid, "current_at_cut"))
        descriptor = self._bootstrap(sources)
        result = self._query(descriptor, sources, kinds=("capture",), query="current_pending")
        self.assertEqual(len(result["items"]), 1)
        item = result["items"][0]
        self.assertEqual(item["reference"], delegated.reference)
        self.assertEqual(item["source_origin_project_commit"], 3)
        self.assertEqual(item["projection_commit"], 7)
        self.assertEqual(item["origin_project_commit"], 7)

    def test_current_delta_preserves_retained_revisions_and_fixed_descriptor_pages(self):
        old = self._source(origin=2)
        sibling = self._source("evidence.second", origin=3)
        first_descriptor = self._bootstrap((old, sibling))
        first_page = self._query(first_descriptor, (old, sibling), page_size=1)
        self.assertEqual(first_page["items"][0]["reference"], old.reference)
        self.assertTrue(first_page["has_more"])

        new = self._source(revision=2, origin=4, content="corrected lemma")
        descriptor = self._delta(first_descriptor, access.OwnerContentDelta(
            removed_current=(old,), added_retained=(new,), added_current=(new,),
        ))
        fixed_page = self._query(
            first_descriptor, (old, sibling), after=first_page["next_after"], page_size=1,
        )
        self.assertEqual([item["reference"] for item in fixed_page["items"]], [sibling.reference])
        self.assertFalse(fixed_page["has_more"])
        current = self._all_pages(descriptor, (old, new, sibling), page_size=1)
        self.assertEqual([item["reference"] for item in current], [new.reference, sibling.reference])
        retained = self._all_pages(
            descriptor, (old, new, sibling), page_size=1, revision_scope="retained_history",
        )
        self.assertEqual([item["reference"] for item in retained], [old.reference, new.reference, sibling.reference])
        access.verify_owner_content_relation(
            self.connection, descriptor=descriptor,
            retained_sources=(old, sibling, new), current_sources=(new, sibling),
        )

    def test_capture_delta_replaces_only_root_occurrence_not_custody_or_delegated_membership(self):
        capture, pending = self._capture()
        sources = access.capture_source_projections(
            project_id=PROJECT, capture=capture, origin_commit=2,
            root_descriptor=pending, projection_commit=3,
        )
        delegated, old, artifact = sources
        descriptor = self._bootstrap(sources)
        _, covered = self._capture(pending=False)
        new = access.capture_source_projections(
            project_id=PROJECT, capture=capture, origin_commit=2,
            root_descriptor=covered, projection_commit=5,
        )[1]
        changed = self._delta(descriptor, access.OwnerContentDelta(
            removed_current=(old,), added_retained=(new,), added_current=(new,),
        ))
        self.assertEqual(self._query(changed, (*sources, new), kinds=("capture",), query="current_pending")["items"], [])
        current = self._query(changed, (*sources, new), kinds=("capture",), query="fully_covered")
        self.assertEqual(current["items"][0]["projection_commit"], 5)
        retained = self._query(
            changed, (*sources, new), kinds=("capture",), query="current_pending",
            revision_scope="retained_history",
        )
        self.assertEqual(retained["items"][0]["projection_commit"], 3)
        delegated_items = self._query(
            changed, (*sources, new), kinds=("capture",), projection_variant="delegated",
            query="", fields=(),
        )["items"]
        self.assertEqual(len(delegated_items), 1)
        self.assertEqual(delegated_items[0]["projection_commit"], 2)
        access.verify_owner_content_relation(
            self.connection, descriptor=changed, retained_sources=(*sources, new),
            current_sources=(delegated, new, artifact),
        )

    def test_prebootstrap_capture_selects_inventory_successor_even_without_lexical_match(self):
        capture, pending = self._capture()
        delegated, old, artifact = access.capture_source_projections(
            project_id=PROJECT, capture=capture, origin_commit=2,
            root_descriptor=pending, projection_commit=2,
        )
        _, covered = self._capture(pending=False)
        new = access.capture_source_projections(
            project_id=PROJECT, capture=capture, origin_commit=2,
            root_descriptor=covered, projection_commit=5,
        )[1]
        sources = (delegated, old, artifact, new)
        descriptor = self._bootstrap(
            sources, current=(delegated, new, artifact), cut=6,
        )

        def no_immutable_head_shortcut(_reference, _cut):
            self.fail("root Capture current selection must use occurrence inventory")

        options = {
            "kinds": ("capture",), "prebootstrap_current": True,
            "head_at_cut": no_immutable_head_shortcut,
        }
        calls = []
        at_four = self._query(
            descriptor, sources, query="current_pending", source_project_commit=4,
            calls=calls, **options,
        )
        self.assertEqual([item["projection_commit"] for item in at_four["items"]], [2])
        self.assertEqual(at_four["items"][0]["reference"], delegated.reference)
        self.assertEqual([item["projection_commit"] for item in calls], [2])
        with self.assertRaisesRegex(WorkspaceIntegrityError, "projection occurrence changed"):
            self._query(
                descriptor, query="current_pending", source_project_commit=4,
                authenticated_source=lambda _item: new, **options,
            )
        calls.clear()
        at_six = self._query(
            descriptor, sources, query="current_pending", source_project_commit=6,
            calls=calls, **options,
        )
        self.assertEqual(at_six["items"], [])
        self.assertEqual(calls, [])
        covered_at_six = self._query(
            descriptor, sources, query="fully_covered", source_project_commit=6,
            **options,
        )
        self.assertEqual([item["projection_commit"] for item in covered_at_six["items"]], [5])
        for cut, expected_occurrence in ((4, 2), (6, 5)):
            with self.subTest(inventory_cut=cut):
                inventory = self._query(
                    descriptor, sources, query="", fields=(),
                    source_project_commit=cut, **options,
                )
                self.assertEqual([item["projection_commit"] for item in inventory["items"]], [expected_occurrence])
        retained = self._query(
            descriptor, sources, kinds=("capture",), query="current_pending",
            revision_scope="retained_history", source_project_commit=6,
        )
        self.assertEqual([item["projection_commit"] for item in retained["items"]], [2])

        occurrence_reads = []

        def exact_occurrence(custody, source_cut, occurrence):
            self.assertEqual(custody, delegated.document)
            occurrence_reads.append((source_cut, occurrence))
            return pending if occurrence == 2 else covered

        with patch.object(access, "read_authenticated_source", return_value=delegated) as custody_read:
            default_path = self._query(
                descriptor, query="current_pending", source_project_commit=4,
                authenticated_source=None, root_capture_projection=exact_occurrence,
                **options,
            )
            self.assertEqual(default_path["items"][0]["capture_descriptor"], pending)
            custody_read.assert_called_once_with(
                self.connection, project_id=PROJECT, reference=delegated.reference,
                indexed_origin_commit=2,
            )
        self.assertEqual(occurrence_reads, [(4, 2)])

    def test_pending_capture_projection_requires_explicit_finalization_including_empty(self):
        capture, pending = self._capture()
        delegated, old, artifact = access.capture_source_projections(
            project_id=PROJECT, capture=capture, origin_commit=2,
            root_descriptor=pending, projection_commit=2,
        )
        sources = (delegated, old, artifact)
        descriptor = self._bootstrap(sources)
        evidence = replace(self._source(origin=5), selected_at_commit=5)
        pending_delta = access.OwnerContentDelta(
            added_retained=(evidence,), added_current=(evidence,),
            pending_capture_projection=True,
        )
        with self.assertRaisesRegex(WorkspaceIntegrityError, "not completed its root Capture projection"):
            self._delta(descriptor, pending_delta)
        _, covered = self._capture(pending=False)
        new = access.capture_source_projections(
            project_id=PROJECT, capture=capture, origin_commit=2,
            root_descriptor=covered, projection_commit=5,
        )[1]
        finalized = access.with_capture_projection_changes(pending_delta, ((old, new),))
        self.assertFalse(finalized.pending_capture_projection)
        self.assertTrue(pending_delta.pending_capture_projection)
        self.assertEqual(finalized.removed_current, (old,))
        self.assertEqual(finalized.added_retained, (evidence, new))
        self.assertEqual(finalized.added_current, (evidence, new))
        changed = self._delta(descriptor, finalized)
        access.verify_owner_content_relation(
            self.connection, descriptor=changed, retained_sources=(*sources, evidence, new),
            current_sources=(delegated, artifact, evidence, new),
        )
        explicitly_empty = access.with_capture_projection_changes(pending_delta, ())
        self.assertFalse(explicitly_empty.pending_capture_projection)
        self.assertEqual(explicitly_empty.added_retained, (evidence,))
        self.assertEqual(explicitly_empty.added_current, (evidence,))
        unchanged_capture = self._delta(descriptor, explicitly_empty)
        access.verify_owner_content_relation(
            self.connection, descriptor=unchanged_capture,
            retained_sources=(*sources, evidence), current_sources=(*sources, evidence),
        )

    def test_lexical_field_union_pages_unique_exact_refs_after_positional_filter(self):
        sources = (
            self._source("evidence.a", content="abcde", title="ABCDE"),
            self._source("evidence.b", content="different", title="abcde"),
            self._source("evidence.c", content="abcde", title="different"),
            self._source("evidence.d", content="abc--bcd--cde", title="different"),
            self._source("evidence.e", content="no matching grams", title="different"),
        )
        descriptor = self._bootstrap(sources)
        calls = []
        items = self._all_pages(
            descriptor, sources, page_size=1, fields=("title", "content", "title"),
            query="AbCdE", calls=calls,
        )
        self.assertEqual([item["reference"] for item in items], [source.reference for source in sources[:3]])
        self.assertEqual([item["matched_fields"] for item in items], [
            ["title", "content"], ["title"], ["content"],
        ])
        self.assertEqual({item["reference"]["identity"] for item in calls}, {"evidence.a", "evidence.b", "evidence.c"})
        # Each page may authenticate one extra match to prove continuation, but
        # neither a gram-only false candidate nor an unrelated source is loaded.
        self.assertLessEqual(len(calls), len(items) + 2)
        self.assertEqual(len({item["handle"] for item in items}), 3)

    def test_inventory_never_loads_source_bodies_and_applies_origin_cut(self):
        sources = (self._source("evidence.a", origin=2), self._source("evidence.b", origin=4))
        descriptor = self._bootstrap(sources)

        def no_source_load(_item):
            self.fail("inventory must not invoke source-body authentication")

        with patch.object(access, "read_authenticated_source", side_effect=AssertionError("body load")):
            items = self._all_pages(
                descriptor, (), page_size=1, query="", fields=(),
                authenticated_source=no_source_load, source_project_commit=3,
                revision_scope="retained_history",
            )
        self.assertEqual([item["reference"] for item in items], [sources[0].reference])
        self.assertEqual(items[0]["matched_fields"], [])

    def test_lexical_candidates_fail_closed_on_wrong_scope_exact_ref_origin_or_field(self):
        source = self._source()
        descriptor = self._bootstrap((source,))
        false_sources = (
            (replace(source, project_id="project.other"), "source/scope"),
            (replace(source, mission_id="mission.other"), "source/scope"),
            (replace(source, revision=2), "source/scope"),
            (replace(source, payload_digest="f" * 64), "source/scope"),
            (replace(source, origin_commit=2), "source origin"),
            (replace(source, fields={"root": {**source.fields["root"], "content": "unrelated"}}), "positional match"),
        )
        for false_source, message in false_sources:
            with self.subTest(false_source=false_source), self.assertRaisesRegex(WorkspaceIntegrityError, message):
                self._query(descriptor, authenticated_source=lambda _item: false_source)

    def test_prebootstrap_current_uses_retained_origins_and_exact_head_selector(self):
        old = self._source(origin=2)
        new = self._source(revision=2, origin=8)
        descriptor = self._bootstrap((old, new), current=(new,))
        head_checks = []

        def head_at_cut(reference, cut):
            head_checks.append((reference, cut))
            return reference == old.reference and cut == 5

        result = self._query(
            descriptor, (old, new), source_project_commit=5,
            prebootstrap_current=True, head_at_cut=head_at_cut,
        )
        self.assertEqual([item["reference"] for item in result["items"]], [old.reference])
        self.assertEqual(head_checks, [(old.reference, 5)])
        with self.assertRaisesRegex(ValueError, "historical head"):
            self._query(descriptor, (old, new), prebootstrap_current=True)

    def test_evidence_creation_selection_and_retirement_have_distinct_historical_cuts(self):
        old = replace(self._source(origin=2), selected_at_commit=2)
        unselected = self._source(revision=2, origin=5, content="corrected lemma")
        created_descriptor = self._bootstrap((old, unselected), current=(old,), cut=5)

        def is_current(descriptor, source, cut):
            return access.source_is_current_at_cut(
                self.connection, project_id=PROJECT, mission_id=MISSION,
                reference=source.reference, cut=cut, descriptor=descriptor,
            )

        self.assertTrue(is_current(created_descriptor, old, 5))
        self.assertFalse(is_current(created_descriptor, unselected, 4))
        self.assertFalse(is_current(created_descriptor, unselected, 5))
        self.assertIsNone(unselected.selected_at_commit)

        selected = replace(unselected, selected_at_commit=6)
        selected_descriptor = self._delta(created_descriptor, access.OwnerContentDelta(
            removed_current=(old,), removed_retained=(unselected,),
            added_retained=(selected,), added_current=(selected,),
        ))
        self.assertEqual(selected.reference, unselected.reference)
        self.assertEqual(selected.origin_commit, 5)
        self.assertEqual(selected.document, unselected.document)
        self.assertEqual(selected.fields, unselected.fields)
        for cut in (2, 4, 5):
            with self.subTest(cut=cut):
                self.assertTrue(is_current(selected_descriptor, old, cut))
                self.assertFalse(is_current(selected_descriptor, selected, cut))
        self.assertFalse(is_current(selected_descriptor, old, 6))
        self.assertTrue(is_current(selected_descriptor, selected, 6))
        # The old sealed descriptor still carries the unselected metadata.
        self.assertTrue(is_current(created_descriptor, old, 5))
        self.assertFalse(is_current(created_descriptor, unselected, 5))

        retired = replace(selected, retired_at_commit=8)
        retired_descriptor = self._delta(selected_descriptor, access.OwnerContentDelta(
            removed_current=(selected,), removed_retained=(selected,),
            added_retained=(retired,),
        ))
        self.assertTrue(is_current(retired_descriptor, retired, 7))
        self.assertFalse(is_current(retired_descriptor, retired, 8))
        self.assertFalse(is_current(retired_descriptor, old, 8))
        self.assertTrue(is_current(selected_descriptor, selected, 7))
        self.assertEqual(retired.reference, selected.reference)

        for cut, expected in ((5, [old.reference]), (6, [selected.reference]),
                              (7, [selected.reference]), (8, [])):
            with self.subTest(historical_current_cut=cut):
                page = self._query(
                    retired_descriptor, (old, retired), source_project_commit=cut,
                    prebootstrap_current=True,
                    head_at_cut=lambda reference, source_cut: access.source_is_current_at_cut(
                        self.connection, project_id=PROJECT, mission_id=MISSION,
                        reference=reference, cut=source_cut, descriptor=retired_descriptor,
                    ),
                )
                self.assertEqual([item["reference"] for item in page["items"]], expected)
        access.verify_owner_content_relation(
            self.connection, descriptor=retired_descriptor,
            retained_sources=(old, retired), current_sources=(),
        )

    def test_retained_selection_metadata_replacement_requires_exact_preimage(self):
        old = replace(self._source(origin=2), selected_at_commit=2)
        unselected = self._source(revision=2, origin=5)
        descriptor = self._bootstrap((old, unselected), current=(old,), cut=5)
        selected = replace(unselected, selected_at_commit=6)
        with self.assertRaisesRegex(WorkspaceIntegrityError, "occurrence addition collision qualifications differ"):
            self._delta(descriptor, access.OwnerContentDelta(added_retained=(selected,)))
        with self.assertRaisesRegex(WorkspaceIntegrityError, "occurrence removal preimage qualifications differ"):
            self._delta(descriptor, access.OwnerContentDelta(removed_retained=(selected,)))
        access.verify_owner_content_relation(
            self.connection, descriptor=descriptor,
            retained_sources=(old, unselected), current_sources=(old,),
        )

    def test_mutation_requires_writer_transaction_and_bootstrap_rejects_wrong_cut(self):
        source = self._source()
        descriptor = index.empty_descriptor(PROFILE_DIGEST)
        with self.assertRaisesRegex(WorkspaceIntegrityError, "writer transaction"):
            access.apply_owner_content_delta(
                self.connection, descriptor=descriptor,
                delta=access.OwnerContentDelta(added_retained=(source,)),
            )
        with self.assertRaisesRegex(WorkspaceIntegrityError, "migration transaction"):
            access.bootstrap_owner_content_index(
                self.connection, project_id=PROJECT, source_project_commit=1,
                retained_sources=(source,), current_sources=(source,),
            )
        for bad in (replace(source, project_id="project.other"), replace(source, origin_commit=2)):
            with self.subTest(source=bad), self.assertRaisesRegex(WorkspaceIntegrityError, "authenticated cut"):
                self._bootstrap((bad,), cut=1)

    def test_complete_relation_audit_detects_source_omission_not_just_valid_graph(self):
        first = self._source()
        second = self._source("evidence.second")
        descriptor = self._bootstrap((first,))
        with self.assertRaisesRegex(WorkspaceIntegrityError, "omitted an eligible occurrence"):
            access.verify_owner_content_relation(
                self.connection, descriptor=descriptor,
                retained_sources=(first, second), current_sources=(first,),
            )
        with self.assertRaisesRegex(WorkspaceIntegrityError, "source audit occurrence qualifications differ"):
            access.verify_owner_content_relation(
                self.connection, descriptor=descriptor,
                retained_sources=(), current_sources=(first,),
            )

    def test_bootstrap_uses_bulk_owner_and_audit_rederives_exact_membership(self):
        first = self._source(revision=2, origin=2, content="Straße qualified lemma")
        second = self._source(revision=10, origin=10, content="changed qualification")
        with patch.object(OwnerContentIndex, "apply_delta", side_effect=AssertionError("incremental bootstrap")):
            descriptor = self._bootstrap((second, first, first), current=(second,), cut=10)
        access.verify_owner_content_relation(
            self.connection, descriptor=descriptor,
            retained_sources=iter((first, second, first)), current_sources=iter((second,)),
        )
        self.assertEqual(
            [item["reference"] for item in self._all_pages(
                descriptor, (first, second), page_size=1, query="qualification",
                revision_scope="retained_history",
            )],
            [second.reference],
        )
        conflicting = self._source(revision=2, origin=2, content="incompatible same revision")
        with self.assertRaises(WorkspaceIntegrityError):
            access.verify_owner_content_relation(
                self.connection, descriptor=descriptor,
                retained_sources=(first, conflicting, second), current_sources=(second,),
            )
        changed = self._source(revision=10, origin=10, content="changed qualification with another scope")
        with self.assertRaises(WorkspaceIntegrityError):
            access.verify_owner_content_relation(
                self.connection, descriptor=descriptor,
                retained_sources=(first, changed), current_sources=(changed,),
            )


class LegacyBranchMissionScopeTests(unittest.TestCase):
    """Real linked-row selection with controlled origin proofs, not custody proof.

    The synthetic predecessor Branch uses the legacy Mission record shape.
    Complete-poststate copies can advance physical revisions
    without changing semantic revisions. Origins and the binding to a supplied
    inventory root are controlled; keyed SQLite reads, row digests, inventory
    traversal, cut selection, topology checks, projection, and persistence are real.
    """

    def setUp(self):
        from research_core import workspace_store
        from research_core.workspace_schema import IdentityKind, TypedWorkspaceId

        self.store = workspace_store
        self.typed_id = lambda kind, identity: TypedWorkspaceId(IdentityKind(kind), identity)
        self.branch_id = "branch.rh.public.1"
        self.strategy_id = "strategy.rh.public.1"
        self.mission_id = "mission.rh.public.1"
        self.connection = sqlite3.connect(":memory:")
        self.connection.row_factory = sqlite3.Row
        self.addCleanup(self.connection.close)
        for kind in ("branch", "strategy", "mission"):
            self.connection.execute(
                f"CREATE TABLE {kind}_revision ("
                "object_id TEXT, revision INTEGER, payload_json TEXT, payload_digest TEXT, "
                "predecessor_revision INTEGER, created_actor TEXT, created_session_id TEXT, "
                "created_evidence_id TEXT, authorization_json TEXT, terminal_history_json TEXT, "
                "created_at TEXT, row_digest TEXT, PRIMARY KEY (object_id, revision))"
            )
        self.connection.execute(
            "CREATE TABLE workspace_metadata (singleton INTEGER PRIMARY KEY, project_id TEXT, "
            "schema_version INTEGER, root_digest_version INTEGER, current_project_commit INTEGER)"
        )
        self.connection.execute(
            "INSERT INTO workspace_metadata VALUES (1, ?, 12, 6, 8)", (PROJECT,),
        )
        self.connection.execute(
            "CREATE TABLE owner_content_index_node ("
            "node_digest TEXT PRIMARY KEY, node_kind TEXT, node_json TEXT)"
        )
        self.origins = {}
        self.branch = {
            "branch_id": self.branch_id, "revision": 1, "project_id": PROJECT,
            "coordination_epoch": "epoch.rh.public.1",
            "current_strategy_id": self.strategy_id, "current_disposition": "continue",
            "disposition_actor_kind": "autonomous_strategy_controller", "lifecycle": "active",
        }
        # Scope-bearing historical Strategy fields, not a mathematical-capsule
        # fixture or a modern Strategy-parser qualification.
        self.strategy = {
            "strategy_id": self.strategy_id, "revision": 2, "mission_id": self.mission_id,
            "branch_id": self.branch_id, "branch_revision": 1, "is_current": True,
        }
        self.mission = {
            "mission_id": self.mission_id, "revision": 1, "project_id": PROJECT,
            "coordination_epoch": "epoch.rh.public.1", "strategy_ids": [self.strategy_id],
        }
        for revision, origin in enumerate((0, 2, 5), 1):
            self._put("branch", self.branch_id, revision, self.branch, origin)
        for revision, origin in enumerate((0, 1, 2, 3, 4), 1):
            self._put("strategy", self.strategy_id, revision, self.strategy, origin)
        self.future_strategy = {**self.strategy, "revision": 3, "branch_revision": 2}
        self._put("strategy", self.strategy_id, 6, self.future_strategy, 8)
        self._put("mission", self.mission_id, 1, self.mission, 0)
        self._put("mission", self.mission_id, 2, self.mission, 3)
        self._put("mission", self.mission_id, 3, {
            **self.mission, "revision": 2, "coordination_epoch": "epoch.future", "strategy_ids": [],
        }, 8)
        self._put("strategy", "strategy.unrelated", 1, {
            "strategy_id": "strategy.unrelated", "branch_id": "branch.unrelated",
            "mission_id": "mission.unrelated", "branch_revision": 1,
        }, 0)
        self.inventory_descriptor = self._build_supplied_inventory()
        self.connection.commit()
        inventory_patch = patch.object(
            self.store, "_schema12_typed_source_inventory", side_effect=self._inventory,
        )
        self.inventory_open = inventory_patch.start()
        self.addCleanup(inventory_patch.stop)
        origin_patch = patch.object(
            self.store, "_schema12_indexed_typed_source_origin", side_effect=self._origin,
        )
        self.sealed_origin = origin_patch.start()
        self.addCleanup(origin_patch.stop)
        historical_patch = patch.object(
            self.store, "_require_historical_typed_revision_origin",
            side_effect=AssertionError("ordinary schema12 scope must use its sealed source route"),
        )
        historical_patch.start()
        self.addCleanup(historical_patch.stop)

    def _put(self, kind, identity, revision, document, origin):
        payload = canonical_json_bytes(document).decode("utf-8")
        values = {
            "object_id": identity, "revision": revision, "payload_json": payload,
            "payload_digest": hashlib.sha256(payload.encode("utf-8")).hexdigest(),
            "predecessor_revision": None if revision == 1 else revision - 1,
            "created_actor": "legacy-scope-fixture", "created_session_id": None,
            "created_evidence_id": None, "authorization_json": "{}",
            "terminal_history_json": "{}", "created_at": "2026-08-24T04:32:53Z",
        }
        values["row_digest"] = self.store._row_digest(f"{kind}_revision", values)
        self.connection.execute(
            f"INSERT OR REPLACE INTO {kind}_revision ({','.join(values)}) "
            f"VALUES ({','.join('?' for _ in values)})", tuple(values.values()),
        )
        self.origins[(kind, identity, revision)] = origin

    def _origin(self, connection, *, project_id, object_id, row, **_options):
        self.assertIs(connection, self.connection)
        self.assertEqual(project_id, PROJECT)
        return self.origins[(object_id.kind.value, object_id.value, row["revision"])]

    def _build_supplied_inventory(self):
        index = OwnerContentIndex(self.connection, PROFILE_DIGEST)

        def inventory():
            for kind in ("branch", "strategy", "mission"):
                for row in self.connection.execute(f"SELECT * FROM {kind}_revision ORDER BY object_id, revision"):
                    document = json.loads(row["payload_json"])
                    identity, revision = row["object_id"], row["revision"]
                    origin = self.origins[(kind, identity, revision)]
                    # This fixture supplies sealed source inventory only;
                    # linked-row scope/projection below still uses the real rows.
                    yield "retained_history", access.SourceProjection(
                        project_id=PROJECT, mission_id=document.get("mission_id", self.mission_id),
                        kind=kind, identity=identity, revision=revision,
                        payload_digest=row["payload_digest"], origin_commit=origin,
                        source_origin_commit=origin, selected_at_commit=origin,
                        document=document,
                        fields={"root": {name: "" for name in PROJECTION_FIELDS["root"][kind]}},
                    )

        return index.bootstrap(inventory())

    def _inventory(self, connection, *, project_id, object_id, metadata, mission_id):
        from research_core.owner_content_index import inventory_directory_key

        self.assertIs(connection, self.connection)
        self.assertEqual(project_id, metadata["project_id"])
        return (
            OwnerContentIndex(self.connection, PROFILE_DIGEST, self.inventory_descriptor),
            inventory_directory_key(project_id, mission_id, "retained_history", "root", object_id.kind.value),
        )

    def _branch_row(self):
        return self.connection.execute(
            "SELECT * FROM branch_revision WHERE object_id = ? AND revision = 3",
            (self.branch_id,),
        ).fetchone()

    def _scope(self):
        return self.store._typed_owner_mission_scope(
            self.connection, project_id=PROJECT,
            object_id=self.typed_id("branch", self.branch_id),
            row=self._branch_row(), origin_commit=5,
        )

    def _read_branch(self):
        row = self._branch_row()
        return access.read_authenticated_source(
            self.connection, project_id=PROJECT,
            reference={"kind": "branch", "identity": self.branch_id, "revision": 3,
                       "payload_sha256": row["payload_digest"]},
            indexed_origin_commit=5,
        )

    def _typed_bytes(self):
        return {
            kind: tuple(tuple(row) for row in self.connection.execute(
                f"SELECT * FROM {kind}_revision ORDER BY object_id, revision",
            )) for kind in ("branch", "strategy", "mission")
        }

    def test_legacy_branch_exact_read_and_bootstrap_preserve_semantic_and_physical_revisions(self):
        before = self._typed_bytes()
        selected = self.store._typed_scope_source_at_cut(
            self.connection, project_id=PROJECT,
            object_id=self.typed_id("strategy", self.strategy_id), cut_project_commit=5,
            mission_id=self.mission_id,
        )
        self.assertIsNotNone(selected)
        self.assertEqual((selected[0]["revision"], selected[1]["revision"], selected[2]), (5, 2, 4))
        self.assertEqual(self._scope(), self.mission_id)
        source = self._read_branch()
        self.assertEqual((source.revision, source.document["revision"], source.origin_commit), (3, 1, 5))
        self.assertEqual(source.mission_id, self.mission_id)
        self.assertEqual(canonical_json_bytes(source.document), canonical_json_bytes(self.branch))
        self.assertNotIn("mission_id", source.document)
        self.assertEqual(source.fields["root"]["content"], "{}")

        # This exercises the actual bootstrap/index consumer of the derived
        # source. Origin custody and full migration enumeration remain outside
        # this controlled-origin test.
        self.connection.execute("BEGIN")
        descriptor = access.bootstrap_owner_content_index(
            self.connection, project_id=PROJECT, source_project_commit=8,
            retained_sources=(source,), current_sources=(),
        )
        self.connection.commit()
        for variant in ("root", "delegated"):
            with self.subTest(variant=variant):
                page = access.query_index_candidates(
                    self.connection, project_id=PROJECT, mission_id=self.mission_id,
                    descriptor=descriptor, projection_variant=variant, kinds=("branch",),
                    fields=() if variant == "root" else ("content",),
                    query="" if variant == "root" else "continue", source_project_commit=8,
                    revision_scope="retained_history",
                )
                self.assertEqual([item["reference"] for item in page["items"]], [source.reference])
                self.assertEqual(page["items"][0]["source_origin_project_commit"], 5)
        self.assertEqual(self._typed_bytes(), before)
        self.assertTrue(self.sealed_origin.called)
        self.assertNotIn("strategy.unrelated", {
            call.kwargs["object_id"].value for call in self.sealed_origin.call_args_list
        })

    def test_legacy_scope_audit_uses_independent_origins_not_the_index_being_built(self):
        from types import SimpleNamespace

        token = self.store._ACTIVE_AUDIT_JOURNAL_INDEX.set(SimpleNamespace(
            connection=self.connection, project_id=PROJECT,
            typed_origins={
                f"{kind}:{identity}@{revision}": (origin,)
                for (kind, identity, revision), origin in self.origins.items()
            },
        ))
        try:
            with patch.object(
                self.store, "_require_historical_typed_revision_origin", side_effect=self._origin,
            ) as historical:
                self.assertEqual(self._scope(), self.mission_id)
                self.assertTrue(historical.called)
                self.connection.execute(
                    "DELETE FROM strategy_revision WHERE object_id = ? AND revision = 6", (self.strategy_id,),
                )
                with self.assertRaisesRegex(WorkspaceIntegrityError, "audited successor"):
                    self._scope()
            self.sealed_origin.assert_not_called()
            self.inventory_open.assert_not_called()
        finally:
            self.store._ACTIVE_AUDIT_JOURNAL_INDEX.reset(token)

    def test_legacy_scope_rejects_wrong_linked_topology_and_unsupported_branch_shapes(self):
        cases = (
            ("branch", self.branch_id, 3, self.branch, {"kind": "mission_branch"}),
            ("branch", self.branch_id, 3, self.branch, {"revision": True}),
            ("branch", self.branch_id, 3, self.branch, {"current_strategy_id": None}),
            ("branch", self.branch_id, 3, self.branch, {"current_strategy_id": "strategy.absent"}),
            ("branch", self.branch_id, 3, self.branch, {"project_id": "project.other"}),
            ("strategy", self.strategy_id, 5, self.strategy, {"strategy_id": "strategy.other"}),
            ("strategy", self.strategy_id, 5, self.strategy, {"mission_id": "mission.other"}),
            ("strategy", self.strategy_id, 5, self.strategy, {"branch_id": "branch.other"}),
            ("strategy", self.strategy_id, 5, self.strategy, {"branch_revision": 3}),
            ("strategy", self.strategy_id, 5, self.strategy, {"branch_revision": True}),
            ("strategy", self.strategy_id, 5, self.strategy, {"is_current": False}),
            ("mission", self.mission_id, 2, self.mission, {"mission_id": "mission.other"}),
            ("mission", self.mission_id, 2, self.mission, {"project_id": "project.other"}),
            ("mission", self.mission_id, 2, self.mission, {"coordination_epoch": "epoch.other"}),
            ("mission", self.mission_id, 2, self.mission, {"strategy_ids": []}),
            ("mission", self.mission_id, 2, self.mission, {"strategy_ids": self.strategy_id}),
        )
        for kind, identity, revision, original, changes in cases:
            with self.subTest(kind=kind, changes=changes):
                origin = self.origins[(kind, identity, revision)]
                self._put(kind, identity, revision, {**original, **changes}, origin)
                try:
                    with self.assertRaises(WorkspaceIntegrityError):
                        self._scope()
                finally:
                    self._put(kind, identity, revision, original, origin)

    def test_legacy_scope_rejects_future_only_links_gaps_and_corrupt_exact_rows(self):
        for kind, identity in (("strategy", self.strategy_id), ("mission", self.mission_id)):
            with self.subTest(future_only=kind):
                saved = dict(self.origins)
                for key in self.origins:
                    if key[:2] == (kind, identity):
                        self.origins[key] = 8
                try:
                    with self.assertRaises(WorkspaceIntegrityError):
                        self._scope()
                finally:
                    self.origins = saved
        self.connection.execute(
            "DELETE FROM strategy_revision WHERE object_id = ? AND revision = 3", (self.strategy_id,),
        )
        with self.assertRaisesRegex(WorkspaceIntegrityError, "revision gap"):
            self._scope()
        self._put("strategy", self.strategy_id, 3, self.strategy, 2)
        self.connection.execute(
            "DELETE FROM strategy_revision WHERE object_id = ? AND revision = 6", (self.strategy_id,),
        )
        with self.assertRaisesRegex(WorkspaceIntegrityError, "authenticated successor"):
            self._scope()
        self._put("strategy", self.strategy_id, 6, self.future_strategy, 8)
        for kind, identity, revision, document in (
            ("branch", self.branch_id, 3, self.branch),
            ("strategy", self.strategy_id, 5, self.strategy),
            ("mission", self.mission_id, 2, self.mission),
        ):
            with self.subTest(corrupt_exact_row=kind):
                origin = self.origins[(kind, identity, revision)]
                self.connection.execute(
                    f"UPDATE {kind}_revision SET payload_json = '{{}}' WHERE object_id = ? AND revision = ?",
                    (identity, revision),
                )
                try:
                    with self.assertRaises(WorkspaceIntegrityError):
                        self._read_branch()
                finally:
                    self._put(kind, identity, revision, document, origin)

    def test_direct_modern_scope_does_not_enter_legacy_link_resolution(self):
        modern = {"schema_version": 1, "kind": "mission_branch", "mission_id": MISSION}
        self._put("branch", self.branch_id, 3, modern, 5)
        with patch.object(
            self.store, "_typed_scope_source_at_cut", side_effect=AssertionError("legacy link lookup"),
        ):
            self.assertEqual(self._scope(), MISSION)
        self.sealed_origin.assert_not_called()

    def test_exact_recovery_wrapper_reuses_derived_scope_after_exact_revision_read(self):
        from types import SimpleNamespace
        from research_core.mission_interface import MissionInterface

        row = self._branch_row()
        stored = self.store.WorkspaceStore._stored_revision(self.typed_id("branch", self.branch_id), row)
        calls = []

        def get_revision(reference):
            calls.append(("revision", reference))
            self.assertEqual(reference, stored.reference)
            return stored

        def read_scope(reference, *, payload_sha256):
            calls.append(("scope", reference, payload_sha256))
            self.assertEqual(reference, stored.reference)
            self.assertEqual(payload_sha256, row["payload_digest"])
            return self._scope()

        # This fixture supplies the existing get_revision custody boundary;
        # it does not qualify WorkspaceStore access authentication itself.
        receiver = SimpleNamespace(
            _mission_id=self.mission_id,
            _store=SimpleNamespace(get_revision=get_revision, _read_typed_owner_mission_scope=read_scope),
        )
        result = MissionInterface._read_recovery_owner_revision(
            receiver, kind="branch", identity=self.branch_id, revision=3,
        )
        self.assertEqual(calls, [
            ("revision", stored.reference), ("scope", stored.reference, row["payload_digest"]),
        ])
        ordinary = self._read_branch()
        self.assertEqual(result["mission_id"], ordinary.mission_id)
        self.assertEqual(result["reference"], ordinary.reference)
        self.assertEqual(canonical_json_bytes(result["document"]), canonical_json_bytes(self.branch))
        self.assertNotIn("mission_id", result["document"])


if __name__ == "__main__":
    unittest.main()
