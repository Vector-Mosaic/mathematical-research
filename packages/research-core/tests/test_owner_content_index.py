from __future__ import annotations

import hashlib
import json
import sqlite3
import sys
import unittest
from contextlib import closing
from itertools import chain, pairwise
from dataclasses import replace
from enum import Enum
from pathlib import Path
from types import MappingProxyType, SimpleNamespace
from unittest import mock


PACKAGE_ROOT = Path(__file__).resolve().parents[1]
if str(PACKAGE_ROOT) not in sys.path:
    sys.path.insert(0, str(PACKAGE_ROOT))

from research_core import owner_content_index as index_module  # noqa: E402
from research_core import owner_content_pages as pages_module  # noqa: E402
from research_core.json_support import canonical_json_bytes  # noqa: E402


PROFILE = hashlib.sha256(b"owner-content-index-unit-fixture").hexdigest()
PROJECT = "project.rh"
MISSION = "mission.index-fixture"


_native_json_bytes = pages_module._native_json_bytes

class IndexIntegrityError(ValueError):
    pass


class _PageObjectFixture(pages_module.PageObjectStore):
    """Bind the production object store to an isolated in-memory table."""

    def __init__(self, connection):
        connection.execute(
            "CREATE TABLE packed_posting_experiment (digest TEXT PRIMARY KEY, kind TEXT, body TEXT)"
        )
        super().__init__(connection, profile_sha256=PROFILE, integrity_error=IndexIntegrityError,
                         table="packed_posting_experiment", digest_column="digest",
                         kind_column="kind", body_column="body",
                         domain="rh.test.packed-posting-discriminator.v1")


class _OccurrenceSearchFixture:
    """Close real occurrence-index search generators after each bounded page."""

    def __init__(self, index, root):
        self.index, self.root_digest = index, root

    def page(self, prefix, query, limit, *, after=None, origin_commit_at_most=None):
        if self.index._depth:
            raise IndexIntegrityError("fixture query cannot share an active operation")
        with closing(self.index.search_field(self.root_digest, prefix, query, after=after,
                     origin_commit_at_most=origin_commit_at_most)) as iterator:
            return index_module.OwnerContentIndex.page(iterator, limit)


def _layout_totals(connection):
    return connection.execute(
        "SELECT COUNT(*),COALESCE(SUM(length(CAST(body AS BLOB))),0) FROM packed_posting_experiment"
    ).fetchone()


class OwnerContentIndexTests(unittest.TestCase):
    """Private access-graph checks, not Store publication or source-custody proof."""

    def _occurrence_audit_oracles(self, experiment, root, source_entries, inventory):
        connection = experiment.packed.connection

        def audit_read_only(selected_root, *, error=None, early_close=False, sources=None, source_only=False):
            for source_shaped in ((True,) if source_only else (False, True)):
                opened, original_enter = [], index_module._SortedEntries.__enter__

                def record_open(sorter):
                    result = original_enter(sorter)
                    opened.append((sorter, Path(sorter._directory.name)))
                    return result

                def run():
                    if source_shaped:
                        return experiment.audit_sources(selected_root, inventory if sources is None else sources)
                    return tuple(experiment.iter_entries(selected_root))

                before = connection.total_changes
                with mock.patch.object(index_module._SortedEntries, "__enter__", new=record_open):
                    if error is not None:
                        with self.assertRaisesRegex((ValueError, RuntimeError), error):
                            run()
                    elif early_close and not source_shaped:
                        iterator = experiment.iter_entries(selected_root)
                        self.assertIsNotNone(next(iterator))
                        self.assertEqual(len(opened), 1)
                        self.assertTrue(opened[0][1].exists())
                        iterator.close()
                    else:
                        result = run()
                        if source_shaped:
                            self.assertEqual(result, len(source_entries))
                self.assertEqual(connection.total_changes, before)
                self.assertEqual(len(opened), 1)
                for sorter, path in opened:
                    self.assertIsNone(sorter._connection)
                    self.assertIsNone(sorter._directory)
                    self.assertFalse(path.exists())
                self.assertEqual((experiment._depth, experiment._cache, experiment._pending), (0, {}, {}))

        lexical = next(entry for entry in source_entries
            if entry[0][0] == "lexical" and entry[0][3:5] == ("current_at_cut", "root")
            and entry[0][6:] == ("content", " c"))
        directory, posting = lexical
        owner = experiment._owner(directory, posting.key)
        identity = owner + (posting.key[2],)
        moved = tuple(entry for entry in source_entries
                      if entry[0][0] == "lexical" and entry[0][4:] == directory[4:] and entry[1].key == posting.key)
        self.assertEqual(len(moved), 2)  # Current and retained exact qualifications.
        for substitute in (False, True):
            connection.execute("SAVEPOINT audit_ordinary_fault")
            try:
                with experiment._operation():
                    roots = experiment._roots(root)
                    ordinary = pages_module.PageTree.replace_source(experiment, roots["ordinary"], (), moved)
                    changes = {("ordinary",): (roots["ordinary"], ordinary)}
                    if substitute:
                        binding = experiment._get(roots["occurrences"], experiment._scope("occurrences"), identity)
                        field_key = directory[6:]
                        before = experiment._get(binding["map"], experiment._map_scope(owner), field_key)
                        self.assertIsNotNone(before)
                        altered_map = experiment._edit(binding["map"], experiment._map_scope(owner), {field_key: (before, None)})
                        altered_binding = {**binding, "map": altered_map}
                        occurrences = experiment._edit(roots["occurrences"], experiment._scope("occurrences"),
                                                       {identity: (binding, altered_binding)})
                        changes[("occurrences",)] = (roots["occurrences"], occurrences)
                        inverse = owner[:4] + field_key + (owner[4],)
                        for name, key in (("current", inverse), ("events", inverse + (identity[-1],))):
                            self.assertEqual(experiment._get(roots[name], experiment._scope(name), key), {"present": True})
                            altered = experiment._edit(roots[name], experiment._scope(name), {key: ({"present": True}, None)})
                            changes[(name,)] = (roots[name], altered)
                    broken = experiment._commit(experiment._edit(root, experiment._ROOT, changes))
                if substitute:
                    # Show the actual escape: the full reconstructed logical
                    # relation still equals source, although this namespace
                    # movement defeats the query inverse. This small fault
                    # oracle intentionally materializes only its tiny fixture.
                    with experiment._operation():
                        broken_roots = experiment._roots(broken)
                        raw_relation = list(pages_module.PageTree.iter_entries(experiment, broken_roots["ordinary"]))
                        for key, value in experiment._items(broken_roots["occurrences"], experiment._scope("occurrences")):
                            prepared = experiment._prepared_occurrence(key, value)
                            selections = [scope for scope, flag in (("current_at_cut", "current"), ("retained_history", "retained")) if value[flag]]
                            raw_relation.extend(experiment._logical_scopes(prepared, selections))
                    canonical = lambda entries: sorted((scope, item.key, item._canonical_bytes()) for scope, item in entries)
                    self.assertEqual(canonical(raw_relation), canonical(source_entries))
                audit_read_only(broken, error="ordinary root has a lexical")
            finally:
                connection.execute("ROLLBACK TO audit_ordinary_fault")
                connection.execute("RELEASE audit_ordinary_fault")

        # Equal actual rows cannot disappear through expected-source dedup.
        before = connection.total_changes
        with index_module._SortedEntries((), integrity_error=IndexIntegrityError) as actual:
            experiment._audit_actual_row(actual._database(), *lexical)
            with self.assertRaisesRegex(IndexIntegrityError, "duplicate actual audit"):
                experiment._audit_actual_row(actual._database(), *lexical)
        self.assertEqual(connection.total_changes, before)
        connection.execute("SAVEPOINT audit_scalar_fault")
        try:
            with experiment._operation():
                roots = experiment._roots(root)
                key = owner[:4] + directory[6:] + (owner[4],)
                # Deliberately rehash the malformed body, rather than asking
                # the valid mutation path to author a non-Boolean membership.
                changed = experiment._tree(experiment._scope("current"),
                    ((found, {"present": 1} if found == key else value)
                     for found, value in experiment._items(roots["current"], experiment._scope("current"))))
                broken = experiment._commit(experiment._edit(root, experiment._ROOT, {("current",): (roots["current"], changed)}))
            audit_read_only(broken, error="current inverse/field-map")
        finally:
            connection.execute("ROLLBACK TO audit_scalar_fault")
            connection.execute("RELEASE audit_scalar_fault")
        with mock.patch.object(experiment, "_inventory_at", side_effect=AssertionError("audit repeated inventory tree read")):
            audit_read_only(root, early_close=True)
        original_occurrences = experiment._audit_occurrences

        def interrupted_occurrences(*args):
            with closing(original_occurrences(*args)) as rows:
                yield next(rows)
                raise RuntimeError("injected audit reconstruction failure")

        with mock.patch.object(experiment, "_audit_occurrences", new=interrupted_occurrences):
            audit_read_only(root, error="audit reconstruction failure")

        # Fresh expectations come from SOURCE, never the stored map or a
        # builder-prepared digest. Exact duplicate source input may coalesce;
        # missing, extra, conflicting and same-digest/different-text input may not.
        audit_read_only(root, sources=inventory + inventory, source_only=True)
        audit_read_only(root, sources=inventory[:-1], source_only=True, error="source audit")
        selected_source = inventory[0][1]
        extra_source = replace(selected_source, identity="context.occurrence.absent")
        audit_read_only(root, sources=inventory + (("retained_history", extra_source),),
                        source_only=True, error="omitted an eligible occurrence")
        for changed_source in (
                replace(selected_source, selected_at_commit=99),
                replace(selected_source, fields={variant: {**fields, "content": "xb c"}
                    for variant, fields in selected_source.fields.items()})):
            changed_inventory = tuple((scope, changed_source if item == selected_source else item)
                                      for scope, item in inventory)
            audit_read_only(root, sources=changed_inventory, source_only=True, error="source audit")
        conflicting = inventory + (("retained_history", replace(selected_source, selected_at_commit=99)),)
        audit_read_only(root, sources=conflicting, source_only=True, error="conflicting|qualifications")

        original_row, original_grams = experiment._audit_actual_row, index_module._field_gram_positions
        derivations = []

        def nonlexical_row(scratch, directory, posting):
            self.assertNotEqual(directory[0], "lexical")
            return original_row(scratch, directory, posting)

        def counted_grams(fields):
            derivations.append(1)
            yield from original_grams(fields)

        with (mock.patch.object(experiment, "_logical_scopes", side_effect=AssertionError("expanded lexical audit")),
              mock.patch.object(experiment, "_audit_actual_row", new=nonlexical_row),
              mock.patch.object(index_module, "_field_gram_positions", new=counted_grams)):
            audit_read_only(root, source_only=True)
        self.assertEqual(len(derivations), len({(item.project_id, item.mission_id, variant,
            item.kind, item.identity, item.revision) for _scope, item in inventory for variant in item.fields}))

        # Rehashed malformed positions include True at the otherwise valid
        # integer position 1; Python equality alone would incorrectly pass it.
        for malformed in ({"positions": [True]}, {"positions": [1, 1]}, {"positions": [4]},
                          {"first": 1, "count": 2, "gaps": [[0, 1]]}):
            connection.execute("SAVEPOINT audit_position_fault")
            try:
                with experiment._operation():
                    roots = experiment._roots(root)
                    binding = experiment._get(roots["occurrences"], experiment._scope("occurrences"), identity)
                    field_key = ("content", "b")
                    before = experiment._get(binding["map"], experiment._map_scope(owner), field_key)
                    self.assertEqual(experiment._vector_positions(before, directory[:6] + field_key, 4), [1])
                    # The valid writer's Python-equality no-op shortcut treats
                    # [True] as [1]. Forge/re-hash the malformed body directly,
                    # as the present:1 oracle above does, then prove its bytes.
                    altered_map = experiment._tree(experiment._map_scope(owner),
                        ((found, malformed if found == field_key else value)
                         for found, value in experiment._items(binding["map"], experiment._map_scope(owner))))
                    self.assertNotEqual(altered_map["digest"], binding["map"]["digest"])
                    self.assertEqual(canonical_json_bytes(experiment._get(altered_map, experiment._map_scope(owner), field_key)),
                                     canonical_json_bytes(malformed))
                    altered = experiment._edit(roots["occurrences"], experiment._scope("occurrences"),
                                              {identity: (binding, {**binding, "map": altered_map})})
                    broken = experiment._commit(experiment._edit(root, experiment._ROOT,
                        {("occurrences",): (roots["occurrences"], altered)}))
                audit_read_only(broken, error="position|invalid|bound")
            finally:
                connection.execute("ROLLBACK TO audit_position_fault")
                connection.execute("RELEASE audit_position_fault")

        for fault in ("inventory", "inventory_qualification", "source", "scope"):
            connection.execute("SAVEPOINT audit_qualification_fault")
            try:
                with experiment._operation():
                    roots = experiment._roots(root)
                    binding = experiment._get(roots["occurrences"], experiment._scope("occurrences"), identity)
                    if fault in {"inventory", "inventory_qualification"}:
                        inventory_directory = ("owner_inventory", *owner[:2], "current_at_cut", *owner[2:4])
                        inventory_posting = pages_module.PageTree.posting(
                            experiment, roots["ordinary"], inventory_directory, posting.key)
                        self.assertIsNotNone(inventory_posting)
                        additions = () if fault == "inventory" else ((inventory_directory,
                            replace(inventory_posting, metadata={**inventory_posting.metadata, "selected_at_commit": 999})),)
                        altered = pages_module.PageTree.replace_source(experiment, roots["ordinary"],
                            ((inventory_directory, inventory_posting),), additions)
                        change = {("ordinary",): (roots["ordinary"], altered)}
                    else:
                        if fault == "source":
                            malformed_source = {**experiment._binding(binding, identity), "source_origin_commit": True}
                            source_digest = experiment._factor({**malformed_source, "positions": [], "field_length": None})["source"]
                            altered_binding = {**binding, "source": source_digest}
                        else:
                            altered_binding = {**binding, "current": 1}
                        if fault == "scope":
                            # True == 1 must not turn this malformed read-side
                            # fixture into an unchanged valid writer result.
                            altered = experiment._tree(experiment._scope("occurrences"),
                                ((found, altered_binding if found == identity else value)
                                 for found, value in experiment._items(roots["occurrences"], experiment._scope("occurrences"))))
                        else:
                            altered = experiment._edit(roots["occurrences"], experiment._scope("occurrences"),
                                                      {identity: (binding, altered_binding)})
                        self.assertNotEqual(altered["digest"], roots["occurrences"]["digest"])
                        self.assertEqual(canonical_json_bytes(experiment._get(altered, experiment._scope("occurrences"), identity)),
                                         canonical_json_bytes(altered_binding))
                        change = {("occurrences",): (roots["occurrences"], altered)}
                    broken = experiment._commit(experiment._edit(root, experiment._ROOT, change))
                audit_read_only(broken, error="inventory|qualifications|binding")
            finally:
                connection.execute("ROLLBACK TO audit_qualification_fault")
                connection.execute("RELEASE audit_qualification_fault")

    def _native_json_guards(self):
        """Native encoding preserves bytes; provenance never broadens its input."""
        texts = ('null', 'true', 'false', '0', '-0.0', '1.25', '1e-100',
                 '1208925819614629174706193', '""', '[]', '{}',
                 r'"Stra\u00dfe \u0130 \u03a3 \ud83d\ude00 \u0000 \\ \""',
                 '{"z":[true,1,null,-0.0],"a":{"nested":[],"text":"Σ"}}')
        for text in texts:
            value = index_module.loads_strict_json(text)
            self.assertEqual(_native_json_bytes(value), canonical_json_bytes(value))
        self.assertNotEqual(_native_json_bytes({"present": True}), _native_json_bytes({"present": 1}))
        for raw in ('{"duplicate":1,"duplicate":2}', '{"nested":{"a":0,"a":1}}',
                    '{"value":NaN}', '{"value":Infinity}', '{"value":-Infinity}'):
            with self.assertRaises(ValueError):
                index_module.loads_strict_json_object(raw)
        for value in (float("inf"), float("-inf"), float("nan"),
                      index_module.loads_strict_json('1e999')):
            for encoder in (canonical_json_bytes, _native_json_bytes):
                with self.assertRaises(ValueError):
                    encoder(value)
        surrogate = index_module.loads_strict_json(r'"\ud800"')
        for encoder in (canonical_json_bytes, _native_json_bytes):
            with self.assertRaises(UnicodeEncodeError):
                encoder(surrogate)

        domain = MappingProxyType({True: MappingProxyType({"sequence": ("Σ", 2 ** 80)})})
        with self.assertRaises(TypeError):
            _native_json_bytes(domain)
        # A builtin dict alone is NOT sufficient provenance: domain key
        # normalization spells True as "True", whereas JSON spells it "true".
        self.assertNotEqual(_native_json_bytes({True: 1}), canonical_json_bytes({True: 1}))
        with closing(sqlite3.connect(":memory:")) as connection:
            packed = _PageObjectFixture(connection)
            for compact in (False, True):
                engine = pages_module.PageTree(packed, compact_page_keys=compact)
                scope = engine._OVERFLOW
                with engine._operation():
                    root = engine._tree(scope, [(("native-provenance", 1), {"chunk": domain})])
                    with mock.patch.object(pages_module, "_native_json_bytes",
                            side_effect=AssertionError("pending domain body entered native encoder")):
                        pending = engine._node(root, scope, children=False)
                        self.assertFalse(engine._object(root["digest"], root["kind"]).native_json)
                        engine._commit(root)
                    expected = canonical_json_bytes(pending)
                with engine._operation(), mock.patch.object(pages_module, "canonical_json_bytes",
                        side_effect=AssertionError("strict decoded body was domain-normalized")):
                    observed = engine._node(root, scope, children=False)
                    self.assertTrue(engine._object(root["digest"], root["kind"]).native_json)
                    self.assertEqual(_native_json_bytes(observed), expected)

            value = {"qualification": "Straße Σ", "count": 2 ** 80, "present": True}

            digest = packed._store("native-guard", value=value)
            body, extent = packed._load_record(digest, "native-guard")
            raw = canonical_json_bytes(body)
            self.assertEqual(extent, len(raw))
            self.assertEqual(_native_json_bytes(body), raw)
            malformed = [json.dumps(body, ensure_ascii=False, indent=2),
                         '{"duplicate":1,"duplicate":2}', '{"value":NaN}', '{"value":1e999}']
            malformed.extend(canonical_json_bytes({**body, name: changed}).decode("utf-8")
                             for name, changed in (("domain", "wrong-domain"),
                                                   ("profile", "0" * 64), ("kind", "wrong-kind")))
            for text in malformed:
                changed_digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
                connection.execute("INSERT OR REPLACE INTO packed_posting_experiment VALUES (?,?,?)",
                                   (changed_digest, "native-guard", text))
                with self.assertRaises(ValueError):
                    packed._load_record(changed_digest, "native-guard")
            connection.execute("INSERT INTO packed_posting_experiment VALUES (?,?,?)",
                               ("f" * 64, "native-guard", raw.decode("utf-8")))
            with self.assertRaises(IndexIntegrityError):
                packed._load_record("f" * 64, "native-guard")

    def _compact_page_guards(self):
        """Bounded codec equivalence/fault cases; no new query or tree engine."""
        from research_core.owner_content_access import OwnerContentDelta, SourceProjection, source_entries

        with closing(sqlite3.connect(":memory:")) as connection:
            packed = _PageObjectFixture(connection)
            plain = pages_module.PageTree(packed, page_bytes=1024, compact_page_keys=False)
            compact = pages_module.PageTree(packed, page_bytes=1024, compact_page_keys=True)
            scope = compact._OVERFLOW  # Generic value leaves need no source links.
            keys = [(), ("a",), ("a", ""), ("a", "Straße", 2), ("a", "İ", 0),
                    ("a", "İ", 2 ** 80 + 17), ("a\0", "Σ", 1)]
            keys += [(PROJECT, MISSION, "root", "capture", "content", "abc",
                      "raw-capture:" + f"{ordinal:048x}", ordinal * 7) for ordinal in range(96)]
            values = [(key, {"chunk": "x" * 32, "ordinal": ordinal}) for ordinal, key in enumerate(sorted(keys))]

            def make(engine, entries):
                with engine._operation():
                    return engine._commit(engine._tree(scope, entries))

            def relation(engine, root):
                with engine._operation():
                    return list(engine._items(root, scope))

            retained = []
            for entries in ([], values[:1], values):
                control, result = make(plain, entries), make(compact, entries)
                self.assertEqual(relation(compact, result), relation(plain, control))
                retained.append((result, entries))
            root = retained[-1][0]
            self.assertGreaterEqual(root["height"], 2)
            with compact._operation():
                unchanged = compact._commit(compact._finish(scope, compact._update(root, scope,
                    {values[10][0]: (values[10][1], values[10][1])})))
            self.assertEqual(unchanged, root)
            before = _layout_totals(connection)
            with compact._operation(), self.assertRaisesRegex(IndexIntegrityError, "preimage"):
                compact._update(root, scope, {values[10][0]: ({"chunk": "wrong"}, None)})
            self.assertEqual(_layout_totals(connection), before)
            with compact._operation():
                sparse = compact._commit(compact._finish(scope, compact._update(root, scope,
                    {key: (value, None) for key, value in values[1:]})))
            self.assertEqual(sparse["height"], 0)
            self.assertEqual(relation(compact, sparse), values[:1])
            with compact._operation():
                empty = compact._commit(compact._finish(scope, compact._update(sparse, scope,
                    {values[0][0]: (values[0][1], None)})))
            self.assertEqual((empty["height"], empty["count"]), (0, 0))
            for retained_root, entries in retained:
                self.assertEqual(relation(compact, retained_root), entries)
            with compact._operation():
                for key, value in values:
                    self.assertEqual(compact._get(root, scope, key), value)
                    self.assertEqual(compact._seek(root, scope, key), (key, value))
                self.assertIsNone(compact._seek(root, scope, values[-1][0], strict=True))
                self.assertIsNone(compact._seek(root, scope, (), reverse=True, strict=True))
                self.assertEqual(compact._seek(root, scope, values[2][0], strict=True), values[3])
                self.assertEqual(compact._seek(root, scope, values[2][0], reverse=True, strict=True), values[1])
            for invalid in ((True,), (False,), (1.0,), (None,), ("safe", True)):
                with self.subTest(key=invalid), self.assertRaisesRegex(IndexIntegrityError, "components"):
                    make(compact, [(invalid, {"chunk": "x"})])

            # Exact packing arithmetic is checked against the canonical encoder,
            # including every prefix/suffix split and both row types.
            leaf_items = [{"key": list(key), "value": compact._encode(value)} for key, value in values]
            original_canonical = canonical_json_bytes
            def no_whole_page_encode(value):
                if isinstance(value, list) or (isinstance(value, dict) and set(value) & {"entries", "children", "key_prefix"}):
                    raise AssertionError("packing re-encoded a complete candidate page")
                return original_canonical(value)
            with mock.patch.object(pages_module, "canonical_json_bytes", new=no_whole_page_encode):
                groups = list(compact._page_groups(leaf_items, "range-cell", prepared=True))
            for group, state in groups:
                with mock.patch.object(compact, "_compact_profile", side_effect=AssertionError("reprofiled finalized page")):
                    payload, used = compact._compact_payload("range-cell", group, prepared=state)
                    with compact._operation():
                        compact._seal(scope, "range-cell", group, prepared=state)
                self.assertEqual(used, len(canonical_json_bytes(payload)))
                self.assertLessEqual(used, compact.page_bytes)
                with self.assertRaisesRegex(IndexIntegrityError, "size accounting"):
                    compact._compact_payload("range-cell", group,
                        prepared=SimpleNamespace(**{**vars(state), "size": state.size + 1}))
            with compact._operation():
                logical = compact._node(root, scope, children=False)
                self.assertEqual(compact._compact_payload(root["kind"], logical["children"])[0],
                    {key: packed._load(root["digest"], root["kind"])[key] for key in ("key_prefix", "children")})
                record = compact._object(root["digest"], root["kind"])
                raw_bytes = len(canonical_json_bytes(record.body))
                logical_bytes = len(canonical_json_bytes(logical))
                self.assertEqual(record.cache_size, raw_bytes + logical_bytes + compact._VALIDATION_FLAG_BYTES)
                self.assertEqual(compact._cache_bytes, record.cache_size)
                with self.assertRaises(IndexIntegrityError):
                    compact._node({**root, "count": root["count"] + 1}, scope, children=False)
                with self.assertRaises(IndexIntegrityError):
                    compact._node(root, ("different-scope",), children=False)
            original_limit = compact.cache_bytes_limit
            try:
                compact.cache_bytes_limit = raw_bytes + compact._VALIDATION_FLAG_BYTES
                with compact._operation():
                    compact._node(root, scope, children=False)
                    self.assertNotIn(("object", root["digest"], root["kind"]), compact._cache)
                    self.assertLessEqual(compact._cache_bytes, compact.cache_bytes_limit)
            finally:
                compact.cache_bytes_limit = original_limit
            original_items_limit = compact.cache_items_limit
            try:
                compact.cache_items_limit = 1
                with compact._operation():
                    compact._node(root, scope)
                    record = compact._object(root["digest"], root["kind"])
                    self.assertEqual(record.cache_size, raw_bytes + logical_bytes + compact._VALIDATION_FLAG_BYTES)
                    self.assertEqual(compact._cache_bytes, record.cache_size)
                    self.assertTrue(record.children)
                    compact._node(root, scope)
                    self.assertEqual(compact._cache_bytes, record.cache_size)
            finally:
                compact.cache_items_limit = original_items_limit

            cell = make(compact, [(("shared", "Σ", 1), {"chunk": "a"}),
                                  (("shared", "Σ", 2 ** 80 + 7), {"chunk": "b"})])

            def forged(original, edit, *, resize=True):
                body = json.loads(canonical_json_bytes(packed._load(original["digest"], original["kind"])))
                edit(body)
                field = "entries" if body["kind"] == "range-cell" else "children"
                if resize:
                    body["used_bytes"] = len(canonical_json_bytes({"key_prefix": body["key_prefix"], field: body[field]}))
                digest = packed._store(body["kind"], **{name: value for name, value in body.items()
                    if name not in {"domain", "profile", "kind"}})
                return {name: body[name] for name in compact._SUMMARY - {"digest"}} | {"digest": digest}

            corruptions = (
                lambda body: body.update(page_codec="unknown-codec"),
                lambda body: body.update(key_prefix="not-a-list"),
                lambda body: body["key_prefix"].append(True),
                lambda body: body["entries"][0].__setitem__(0, [True]),
                lambda body: body["entries"][0].append(0),
                lambda body: body["entries"].reverse(),
                lambda body: body["entries"].__setitem__(1, body["entries"][0]),
                lambda body: body.update(count=body["count"] + 1),
                lambda body: body.update(used_bytes=0),
            )
            for ordinal, edit in enumerate(corruptions):
                broken = forged(cell, edit, resize=ordinal != len(corruptions) - 1)
                with self.subTest(corruption=ordinal), self.assertRaises(IndexIntegrityError):
                    relation(compact, broken)
                self.assertEqual((compact._cache, compact._pending, compact._depth), ({}, {}, 0))
            # A nonmaximal prefix reconstructs the same keys but is not this
            # codec's single canonical form. This is not a compression-ratio rule.
            def noncanonical(body):
                last = body["key_prefix"].pop()
                for entry in body["entries"]:
                    entry[0].insert(0, last)
            with self.assertRaisesRegex(IndexIntegrityError, "canonical"):
                relation(compact, forged(cell, noncanonical))
            huge = forged(cell, lambda body: body["entries"].extend(body["entries"] * 1000))
            with mock.patch.object(compact, "_common_key_prefix", side_effect=AssertionError("expanded oversized page")):
                with self.assertRaisesRegex(IndexIntegrityError, "page bytes"):
                    relation(compact, huge)
            for edit in (lambda body: body["children"][0].__setitem__(2, True),
                         lambda body: body["children"][0].__setitem__(3, [True]),
                         lambda body: body.update(first=[True])):
                with self.assertRaises(IndexIntegrityError):
                    relation(compact, forged(root, edit))
            with compact._operation(), mock.patch.object(compact, "_common_key_prefix",
                    side_effect=AssertionError("expanded wrong-scope page")):
                with self.assertRaisesRegex(IndexIntegrityError, "scope/summary"):
                    compact._node(root, ("not-the-authenticated-scope",))
            with self.assertRaises(IndexIntegrityError):
                relation(plain, cell)
            with compact._operation():
                self.assertEqual(list(compact._items(make(plain, values[:1]), scope)), values[:1])

            connection.execute("SAVEPOINT compact_fault")
            try:
                with compact._operation():
                    body = compact._node(root, scope, children=False)
                    unvisited = body["children"][-1]
                    connection.execute("UPDATE packed_posting_experiment SET body='{}' WHERE digest=?", (unvisited["digest"],))
                    with self.assertRaises(IndexIntegrityError):
                        compact._node(root, scope, children=True)
            finally:
                connection.execute("ROLLBACK TO compact_fault")
                connection.execute("RELEASE compact_fault")
            relation(compact, cell)
            connection.execute("SAVEPOINT compact_cold_fault")
            try:
                connection.execute("DELETE FROM packed_posting_experiment WHERE digest=?", (cell["digest"],))
                with self.assertRaises(IndexIntegrityError):
                    relation(compact, cell)
            finally:
                connection.execute("ROLLBACK TO compact_cold_fault")
                connection.execute("RELEASE compact_cold_fault")

            # Source-qualified candidate proof, not merely self-roundtripping
            # encoded tuples. The ordinary logical relation is independently
            # rederived from supplied fields for both current and retained cuts.
            def source(revision, text):
                fields = {"id": "context.compact", "title": "qualified Σ", "content": text,
                          "relationships": "positive scale only; parent program preserved"}
                document = {"account": text, "known_omissions": ["No production custody claim."]}
                return SourceProjection(PROJECT, MISSION, "context", "context.compact", revision,
                    hashlib.sha256(canonical_json_bytes(document)).hexdigest(), revision, document,
                    {"root": fields, "delegated": {**fields, "content": "delegated: " + text}}, selected_at_commit=revision)

            first, second = source(2, "abc Straße İ " + "x" * 400), source(9, "shift abc Straße İ " + "x" * 400)
            initial = (("current_at_cut", first), ("retained_history", first))
            final = (("current_at_cut", second), ("retained_history", first), ("retained_history", second))
            expected = {"before": list(entry for selected, item in initial for entry in source_entries(item, selected)),
                        "after": list(entry for selected, item in final for entry in source_entries(item, selected))}
            # Discard only the prepared handoff in the control. It exercises
            # the former direct-sealing route through the SAME writer, rather
            # than copying a second codec or comparing merely logical output.
            with (closing(sqlite3.connect(":memory:")) as prepared_db,
                  closing(sqlite3.connect(":memory:")) as unprepared_db):
                parity_packed = [_PageObjectFixture(db) for db in (prepared_db, unprepared_db)]
                parity = [pages_module.PageTree(store, page_bytes=1024, compact_page_keys=True)
                          for store in parity_packed]

                def stored_bytes(db):
                    return db.execute("SELECT digest,kind,body FROM packed_posting_experiment ORDER BY digest").fetchall()

                def equal_physical(roots):
                    self.assertEqual(roots[0], roots[1])
                    self.assertEqual(stored_bytes(prepared_db), stored_bytes(unprepared_db))

                direct_seal = parity[1]._seal
                def unprepared_seal(selected_scope, kind, items, *, prepared=None):
                    return direct_seal(selected_scope, kind, items)

                with mock.patch.object(parity[1], "_seal", new=unprepared_seal):
                    for entries in ([], values[:1], values):
                        roots = [make(engine, entries) for engine in parity]
                        equal_physical(roots)
                    for changes in (
                        {values[10][0]: (values[10][1], {"chunk": "edited", "ordinal": 10})},
                        {key: (value, None) for key, value in values[1:] if key != values[10][0]},
                        {values[10][0]: ({"chunk": "edited", "ordinal": 10}, None)},
                        {values[0][0]: (values[0][1], None)},
                    ):
                        for ordinal, engine in enumerate(parity):
                            with engine._operation():
                                roots[ordinal] = engine._commit(engine._finish(scope,
                                    engine._update(roots[ordinal], scope, changes)))
                        equal_physical(roots)
                occurrences = [pages_module.OccurrenceIndex(parity_packed[0]),
                               pages_module.OccurrenceIndex(parity_packed[1], compact_page_keys=True)]
                direct_seal = occurrences[1]._seal
                with mock.patch.object(occurrences[1], "_seal", new=unprepared_seal):
                    roots = [engine.build(initial) for engine in occurrences]
                    equal_physical(roots)
                    roots = [engine.replace_source(selected_root, OwnerContentDelta(
                        removed_current=(first,), added_current=(second,), added_retained=(second,)))
                        for engine, selected_root in zip(occurrences, roots)]
                    equal_physical(roots)
            candidate = pages_module.OccurrenceIndex(packed)
            connection.commit()
            connection.execute("BEGIN")
            from research_core.observability import preparation_progress_sink
            progress = []
            with preparation_progress_sink(progress.append):
                initial_root = candidate.build(initial)
            final_progress = {item["phase"]: item for item in progress}
            groups = {(item.project_id, item.mission_id, variant, item.kind, item.identity, item.revision)
                      for _selected, item in initial for variant in item.fields}
            ordinary = {(directory, posting.key) for directory, posting in expected["before"]
                        if directory[0] != "lexical"}
            for phase, total in (("index_source_ingest", len(initial)),
                    ("index_occurrence_build", len(groups)), ("index_ordinary_build", len(ordinary)),
                    ("index_directory_build", len({directory for directory, _key in ordinary}) + 5)):
                self.assertEqual(final_progress[phase], {"phase": phase, "completed": total, "total": total})
            self.assertEqual(next(item for item in progress if item["phase"] == "index_occurrence_build"),
                             {"phase": "index_occurrence_build", "completed": 0, "total": len(groups)})
            final_root = candidate.replace_source(initial_root, OwnerContentDelta(
                removed_current=(first,), added_current=(second,), added_retained=(second,)))
            for candidate_root, inventory, label in ((initial_root, initial, "before"), (final_root, final, "after")):
                progress.clear()
                with preparation_progress_sink(progress.append):
                    self.assertEqual(candidate.audit_sources(candidate_root, inventory), len(expected[label]))
                final_progress = {item["phase"]: item for item in progress}
                self.assertEqual(final_progress["index_audit_sources"], {"phase": "index_audit_sources",
                                 "completed": len(inventory), "total": len(inventory)})
                groups = {(item.project_id, item.mission_id, variant, item.kind, item.identity, item.revision)
                          for _selected, item in inventory for variant in item.fields}
                self.assertEqual(final_progress["index_audit_occurrences"], {"phase": "index_audit_occurrences",
                                 "completed": len(groups), "total": len(groups)})
                observed = [(directory, posting.key, posting._canonical_bytes()) for directory, posting in candidate.iter_entries(candidate_root)]
                wanted = sorted((directory, posting.key, posting._canonical_bytes()) for directory, posting in expected[label])
                self.assertEqual(observed, wanted)
                for selected in ("current_at_cut", "retained_history"):
                    prefix = ("lexical", PROJECT, MISSION, selected, "root", "context", "content")
                    view = _OccurrenceSearchFixture(candidate, candidate_root)
                    keys, after = [], None
                    while True:
                        page, more = view.page(prefix, "STRASSE", 1, after=after)
                        keys.extend(posting.key for posting in page)
                        self.assertEqual((candidate._cache, candidate._pending, candidate._depth), ({}, {}, 0))
                        if not more:
                            break
                        after = page[-1].key
                    self.assertEqual(keys, sorted((item.kind, item.identity, item.revision)
                        for scope_name, item in inventory if scope_name == selected))
            for experiment in (compact, candidate):
                self.assertEqual((experiment._cache, experiment._pending, experiment._depth), ({}, {}, 0))
                self.assertLessEqual(experiment.peak_cache_items, experiment.cache_items_limit)
                self.assertLessEqual(experiment.peak_cache_bytes, experiment.cache_bytes_limit)
            return {"plain_relation_parity": 1, "source_relation_and_qualifications": 1,
                "canonical_grammar_and_type_refusal": 1, "preexpansion_bound_verified": 1,
                "split_merge_collapse_noop_old_roots": 1, "missing_cold_and_unrouted_child_refusal": 1,
                "combined_cache_charge_and_overbudget_refusal": 1, "page_close_and_continuation": 1,
                "growth_height": root["height"], "growth_memberships": len(values)}

    def test_compact_page_key_codec_guards(self):
        self._native_json_guards()
        self._compact_page_guards()
        self._production_page_owner_guards()

    def _production_page_owner_guards(self):
        """The extracted owner receives schema/profile/error; it creates none."""
        arguments = dict(profile_sha256=PROFILE, integrity_error=IndexIntegrityError,
            table="production_page_fixture", digest_column="object_digest",
            kind_column="object_kind", body_column="object_json", domain="production-page-fixture")
        pure_store = pages_module.PageObjectStore(None, **arguments)
        pure = pages_module.OccurrenceIndex(pure_store)
        expected = pure.empty_root()
        self.assertEqual((pure_store.node_reads, pure_store.nodes_written), (0, 0))
        self.assertEqual((pure._cache, pure._pending, pure._depth), ({}, {}, 0))
        with pure.operation():
            value = {"payload": "x" * 8192}
            encoded = pure._encode(value)
            self.assertEqual(pure._decode(encoded), value)
            self.assertIsNotNone(pure._cached(("value", encoded["overflow"]["digest"],
                                              encoded["sha256"], encoded["size"])))
            forged = {**encoded, "overflow": {**encoded["overflow"],
                                               "count": encoded["overflow"]["count"] + 1}}
            with self.assertRaisesRegex(IndexIntegrityError, "summary"):
                pure._decode(forged)
        self.assertEqual((pure._cache, pure._pending, pure._depth), ({}, {}, 0))
        with self.assertRaisesRegex(IndexIntegrityError, "caller transaction"):
            pure.empty_root(persist=True)
        with closing(sqlite3.connect(":memory:")) as connection:
            store = pages_module.PageObjectStore(connection, **arguments)
            self.assertEqual(connection.execute("SELECT name FROM sqlite_master").fetchall(), [])
            connection.execute("CREATE TABLE production_page_fixture "
                               "(object_digest TEXT PRIMARY KEY, object_kind TEXT, object_json TEXT)")
            engine = pages_module.OccurrenceIndex(store, cache_items_limit=1024,
                                                 cache_bytes_limit=8 * 1024 * 1024)
            connection.execute("BEGIN")
            self.assertEqual(engine.empty_root(persist=True), expected)
            rows = connection.execute("SELECT * FROM production_page_fixture ORDER BY object_digest").fetchall()
            self.assertEqual(store.nodes_written, len(rows))
            self.assertEqual(engine.build(()), expected)
            self.assertEqual(connection.execute("SELECT * FROM production_page_fixture ORDER BY object_digest").fetchall(), rows)
            self.assertEqual(store.nodes_written, len(rows))
            self.assertEqual(engine.audit_sources(expected, ()), 0)
            self.assertGreater(store.node_reads, 0)
            before = store.node_reads
            digest, kind, raw = rows[0]
            connection.execute("UPDATE production_page_fixture SET object_json=? WHERE object_digest=?",
                               ('{"duplicate":1,"duplicate":2}', digest))
            with self.assertRaisesRegex(IndexIntegrityError, "invalid object JSON"):
                store._load_record(digest, kind)
            self.assertEqual(store.node_reads, before + 1)
            connection.rollback()
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM production_page_fixture").fetchone()[0], 0)

    def test_occurrence_index_source_updates_and_fault_guards(self):
        """Exercise selected-index updates and faults without prototype comparisons."""
        from research_core import owner_content_access as access_module
        from research_core.owner_content_access import OwnerContentDelta, SourceProjection, capture_source_projections, source_entries

        def source(ordinal, revision, content):
            owner = f"context.occurrence.{ordinal:02}"
            fields = {"id": owner, "title": "qualified scale", "content": content,
                      "relationships": "requires positive-rooted scale; does not exclude the parent program"}
            document = {"account": content, "known_omissions": ["contour normalization remains qualified"]}
            return SourceProjection(PROJECT, MISSION, "context", owner, revision,
                hashlib.sha256(canonical_json_bytes(document)).hexdigest(), revision, document,
                {"root": fields, "delegated": {**fields, "content": "delegated: " + content}},
                selected_at_commit=revision)

        def qualified_sources(current, retained):
            return tuple((selected, item) for selected, sources in (("current_at_cut", current), ("retained_history", retained))
                         for item in sources)

        def all_entries(current, retained):
            return tuple(entry for selected, item in qualified_sources(current, retained)
                         for entry in source_entries(item, selected))

        initial = [source(ordinal, 2, (f"abcde Straße common owner {ordinal}; " + "a" * 256)[:256]) for ordinal in range(8)]
        # Exact 256-codepoint accounts; lexical diversity is not inferred from
        # numeric owner labels. A single rare marker exercises stream seeking.
        initial[-1] = source(7, 2, initial[-1].fields["root"]["content"][:-4] + " qzx")
        current, retained = list(initial), list(initial)
        cuts, deltas = [(tuple(current), tuple(retained))], []
        contents = (initial[3].fields["root"]["content"].replace("abcde", "abxde"),
                    initial[3].fields["root"]["content"] + " qzx",
                    "prefix shift " + initial[3].fields["root"]["content"] + " qzx")
        for revision, content in zip((7, 11, 20), contents):
            old, new = current[3], source(3, revision, content)
            removed = tuple(source_entries(old, "current_at_cut"))
            added = tuple(chain(source_entries(new, "retained_history"), source_entries(new, "current_at_cut")))
            deltas.append((removed, added, OwnerContentDelta(
                removed_current=(old,), added_retained=(new,), added_current=(new,))))
            current[3] = new
            retained.append(new)
            cuts.append((tuple(current), tuple(retained)))
        expected = [all_entries(*cut) for cut in cuts]

        def assert_relation(experiment, root, entries, inventory=None):
            wanted = sorted((directory, posting.key, posting._canonical_bytes()) for directory, posting in entries)
            actual = [(directory, posting.key, posting._canonical_bytes()) for directory, posting in experiment.iter_entries(root)]
            self.assertEqual(actual, wanted)
            if inventory is not None:
                self.assertEqual(experiment.audit_sources(root, inventory), len(wanted))

        query_specs = [(selected, variant, query) for selected, variant in (
            ("current_at_cut", "root"), ("retained_history", "delegated"))
            for query in ("abcde", "qzx", "STRASSE", "common qzx", "a", "never-present")]

        def query(experiment, root, cut, selected, variant, text):
            sources = cut[0] if selected == "current_at_cut" else cut[1]
            wanted = sorted((item.kind, item.identity, item.revision) for item in sources
                            if variant in item.fields and text.casefold() in item.fields[variant]["content"].casefold())
            prefix = ("lexical", PROJECT, MISSION, selected, variant, "context", "content")
            reader, found, cursor = _OccurrenceSearchFixture(experiment, root), [], None
            while True:
                page, more = reader.page(prefix, text, 1, after=cursor)
                found.extend(posting.key for posting in page)
                if not more:
                    break
                self.assertEqual(len(page), 1)
                self.assertNotEqual(cursor, page[-1].key)
                cursor = page[-1].key
            self.assertEqual(found, wanted)

        with closing(sqlite3.connect(":memory:")) as connection:
            packed = _PageObjectFixture(connection)
            experiment = pages_module.OccurrenceIndex(packed, compact_page_keys=False)
            connection.execute("BEGIN")  # The caller owns commit/rollback.
            root = experiment.build(qualified_sources(*cuts[0]))
            bootstrap_buffers = dict(experiment.bootstrap_buffers)
            roots = [root]
            for removals, additions, source_delta in deltas:
                # Observe the existing source updates, not another
                # replay. Recursive _get calls are not new probes.
                original_get, get_depth, probes = experiment._get, 0, {}

                def ordered_event_get(tree, scope, key):
                    nonlocal get_depth
                    outer = get_depth == 0
                    get_depth += 1
                    try:
                        value = original_get(tree, scope, key)
                    finally:
                        get_depth -= 1
                    if outer and scope == experiment._scope("events"):
                        identity = key[:4] + key[6:]
                        probes.setdefault(identity, []).append(key)
                        # These three updates add fresh retained keys;
                        # negative preimages must still be read.
                        self.assertIsNone(value)
                    return value

                identities = {directory[1:3] + directory[4:] + posting.key[1:]
                              for directory, posting in additions
                              if directory[0] == "owner_inventory" and directory[3] == "retained_history"}
                wanted_probes = {identity: sorted({identity[:4] + directory[6:] + identity[4:]
                    for directory, posting in chain(removals, additions)
                    if directory[0] == "lexical"
                    and (directory[1], directory[2], directory[4], directory[5], posting.key[1]) == identity[:-1]})
                    for identity in identities}
                with (mock.patch.object(experiment, "_get", new=ordered_event_get),
                      mock.patch.object(access_module, "source_entries", side_effect=AssertionError("expanded source update")),
                      mock.patch.object(index_module, "entries_for_fields", side_effect=AssertionError("lexical Posting update")),
                      mock.patch.object(experiment, "_logical_scopes", side_effect=AssertionError("Posting reconstruction update"))):
                    root = experiment.replace_source(root, source_delta)
                self.assertTrue(wanted_probes)
                self.assertTrue(all(wanted_probes.values()))
                self.assertEqual(probes, wanted_probes)
                self.assertEqual(get_depth, 0)
                roots.append(root)
            for old_root, entries in zip(roots, expected):
                assert_relation(experiment, old_root, entries)
            for old_root, cut, entries in zip(roots, cuts, expected):
                self.assertEqual(experiment.audit_sources(old_root, qualified_sources(*cut)), len(entries))
            for spec in query_specs:
                query(experiment, root, cuts[-1], *spec)
            query(experiment, roots[0], cuts[0], "retained_history", "root", "abcde")
            retained_objects, retained_bytes = _layout_totals(connection)
            self.assertEqual(bootstrap_buffers["pending_objects_peak"], 0)
            self.assertLessEqual(bootstrap_buffers["tail_payload_bytes_peak"], 2 * experiment.page_bytes)
            self.assertLess(bootstrap_buffers["source_group_logical_memberships_peak"], len(expected[0]))
            self.assertEqual(bootstrap_buffers["source_groups"], 2 * len(initial))
            self.assertEqual(bootstrap_buffers["logical_memberships"], len(expected[0]))
            self.assertIsNone(experiment._bootstrap_scratch)
            self.assertFalse(experiment._bootstrap_cursors)
            # Small source-derived equivalence oracle, not a second
            # builder: explicitly invoke the former empty+delta route.
            # Bulk shape MAY differ; complete logical meaning may not.
            def small_source(revision, text):
                item = source(90, revision, text)
                return replace(item, fields={variant: {
                    field: text if field == "content" else "" for field in fields
                } for variant, fields in item.fields.items()})

            small_old, small_empty, small_new = small_source(2, "ab"), small_source(5, ""), small_source(10, "ab c")
            small_entries = all_entries((small_new,), (small_old, small_empty, small_new))
            small_inventory = qualified_sources((small_new,), (small_old, small_empty, small_new))

            def source_build(inventory, *, error=None):
                # Bootstrap owns scratch now; prove that ownership on
                # both success and exceptions, without passing it an
                # already-expanded Posting/sort oracle.
                from research_core import owner_content_access as access_module
                opened, original_enter = [], index_module._SortedEntries.__enter__

                def record_open(sorter):
                    result = original_enter(sorter)
                    opened.append((sorter, Path(sorter._directory.name)))
                    return result

                with (mock.patch.object(index_module._SortedEntries, "__enter__", new=record_open),
                      mock.patch.object(access_module, "source_entries", side_effect=AssertionError("expanded source bootstrap")),
                      mock.patch.object(index_module, "entries_for_fields", side_effect=AssertionError("lexical Posting bootstrap")),
                      mock.patch.object(experiment, "_logical_scopes", side_effect=AssertionError("Posting reconstruction bootstrap"))):
                    if error is None:
                        built = experiment.build(inventory)
                    else:
                        with self.assertRaisesRegex((ValueError, RuntimeError), error):
                            experiment.build(inventory)
                        built = None
                self.assertEqual(len(opened), 1)
                for sorter, path in opened:
                    self.assertIsNone(sorter._connection)
                    self.assertIsNone(sorter._directory)
                    self.assertFalse(path.exists())
                self.assertIsNone(experiment._bootstrap_scratch)
                self.assertEqual((experiment._depth, experiment._cache, experiment._pending, experiment._bootstrap_cursors),
                                 (0, {}, {}, set()))
                return built

            connection.execute("SAVEPOINT bootstrap_equivalence")
            try:
                empty_source_root = source_build(())
                self.assertEqual(experiment.bootstrap_buffers["logical_memberships"], 0)
                assert_relation(experiment, empty_source_root, (), ())
                bulk_root = source_build(reversed(small_inventory))
                self.assertEqual(experiment.bootstrap_buffers["logical_memberships"], len(small_entries))
                equal_root = source_build(small_inventory + small_inventory)
                self.assertEqual(equal_root, bulk_root)
                self.assertEqual(experiment.bootstrap_buffers["logical_memberships"], len(small_entries))
                retired_sources = (small_old, small_empty, replace(small_new, retired_at_commit=11))
                retired_inventory = qualified_sources((), retired_sources)
                retired_entries = all_entries((), retired_sources)
                retired_bulk = source_build(retired_inventory)
                self.assertEqual(experiment.bootstrap_buffers["logical_memberships"], len(retired_entries))
                assert_relation(experiment, retired_bulk, retired_entries, retired_inventory)
                self.assertEqual(query(experiment, retired_bulk, ((), retired_sources),
                                       "current_at_cut", "root", "ab"), (1, 0))
                with experiment._operation():
                    empty_roots = {name: experiment._tree(experiment._scope(name), []) for name in experiment._NAMES}
                    empty_root = experiment._tree(experiment._ROOT, [((name,), empty_roots[name]) for name in experiment._NAMES])
                    delta_root = experiment.replace_source(empty_root, OwnerContentDelta(
                        added_retained=(small_old, small_empty, small_new), added_current=(small_new,)))
                    experiment._commit(delta_root)
                assert_relation(experiment, bulk_root, small_entries, small_inventory)
                assert_relation(experiment, delta_root, small_entries, small_inventory)
                query(experiment, bulk_root, ((small_new,), (small_old, small_empty, small_new)),
                      "retained_history", "root", "ab")
                # A genuine source cannot omit its inventory. Test
                # missing inventory at the authenticated GRAPH owner,
                # rather than manufacturing a partial SourceProjection.
                missing_inventory = next(entry for entry in small_entries
                    if entry[0][0] == "owner_inventory" and entry[0][3:5] == ("current_at_cut", "root"))
                with experiment._operation():
                    source_roots = experiment._roots(bulk_root)
                    ordinary = pages_module.PageTree.replace_source(experiment,
                        source_roots["ordinary"], (missing_inventory,), ())
                    broken = experiment._commit(experiment._edit(bulk_root, experiment._ROOT,
                        {("ordinary",): (source_roots["ordinary"], ordinary)}))
                with self.assertRaisesRegex(IndexIntegrityError, "inventory is absent"):
                    tuple(experiment.iter_entries(broken))
                with self.assertRaisesRegex(IndexIntegrityError, "inventory is absent"):
                    experiment.audit_sources(broken, small_inventory)
                self._occurrence_audit_oracles(experiment, bulk_root, small_entries, small_inventory)
            finally:
                connection.execute("ROLLBACK TO bootstrap_equivalence")
                connection.execute("RELEASE bootstrap_equivalence")
            self.assertEqual(_layout_totals(connection), (retained_objects, retained_bytes))
            # Failure after final pages have actually been written:
            # scratch/operation cleanup is local, rollback is caller-
            # owned and preserves its preexisting retained graph.
            stores, original_store = [0], packed._store

            def interrupted_store(kind, **payload):
                digest = original_store(kind, **payload)
                if kind == "range-cell":
                    stores[0] += 1
                    if stores[0] == 3:
                        raise RuntimeError("injected final-page bootstrap failure")
                return digest

            connection.execute("SAVEPOINT bootstrap_failure")
            try:
                with mock.patch.object(packed, "_store", new=interrupted_store):
                    source_build(small_inventory, error="final-page")
                self.assertEqual(stores[0], 3)
                self.assertGreater(_layout_totals(connection)[0], retained_objects)
                self.assertTrue(connection.in_transaction)
                self.assertEqual((experiment._depth, experiment._cache, experiment._pending, experiment._bootstrap_cursors),
                                 (0, {}, {}, set()))
            finally:
                connection.execute("ROLLBACK TO bootstrap_failure")
                connection.execute("RELEASE bootstrap_failure")
            self.assertEqual(_layout_totals(connection), (retained_objects, retained_bytes))
            # Valid empty presence predicates isolate the two-head
            # guard. With presence true the conflicting unrevisioned
            # membership must independently fail before that guard.
            multiple_heads = tuple(("current_at_cut", replace(item,
                document={**item.document, "known_omissions": []})) for item in (small_old, small_new))
            changed_fields = replace(small_old, fields={variant: {**fields, "content": "different"}
                for variant, fields in small_old.fields.items()})
            invalid_inputs = (
                ((("current_at_cut", object()),), "source projections"),
                ((("unsupported", small_old),), "revision scope"),
                (multiple_heads, "current heads"),
                (qualified_sources((small_old, small_new), ()), "membership collision"),
                ((("current_at_cut", small_old), ("retained_history", replace(small_old, selected_at_commit=99))), "qualifications"),
                ((("retained_history", small_old), ("retained_history", replace(small_old, selected_at_commit=99))), "membership collision"),
                ((("retained_history", small_old), ("retained_history", changed_fields)), "qualifications"),
            )
            for invalid, message in invalid_inputs:
                connection.execute("SAVEPOINT bootstrap_invalid")
                try:
                    source_build(invalid, error=message)
                    self.assertTrue(connection.in_transaction)
                finally:
                    connection.execute("ROLLBACK TO bootstrap_invalid")
                    connection.execute("RELEASE bootstrap_invalid")
            self.assertEqual(_layout_totals(connection), (retained_objects, retained_bytes))
            # A closed caller transaction is rejected before any
            # physical write, independently of the main retained DB.
            with closing(sqlite3.connect(":memory:")) as unowned:
                unowned_packed = _PageObjectFixture(unowned)
                unowned_view = pages_module.OccurrenceIndex(unowned_packed, compact_page_keys=False)
                with mock.patch.object(index_module._SortedEntries, "__enter__", side_effect=AssertionError("unowned scratch")):
                    with self.assertRaisesRegex(IndexIntegrityError, "caller transaction"):
                        unowned_view.build(small_inventory)
                self.assertEqual(_layout_totals(unowned), (0, 0))
            guarded_prefix = ("lexical", PROJECT, MISSION, "retained_history", "root", "context", "content")
            with mock.patch.object(experiment, "_roots", side_effect=AssertionError("invalid cut read graph")):
                for invalid_cut in (-1, True, "2", 1.5):
                    with self.assertRaisesRegex(ValueError, "origin cut"):
                        tuple(experiment.search_field(root, guarded_prefix, "a", origin_commit_at_most=invalid_cut))
            bounded = tuple(experiment.search_field(root, list(guarded_prefix), "a", origin_commit_at_most=11))
            self.assertEqual([posting.key for posting in bounded], sorted(
                (item.kind, item.identity, item.revision) for item in retained
                if item.origin_commit <= 11 and "a" in item.fields["root"]["content"].casefold()))
            reuse = {"map_walks": 0, "vector_expansions": 0,
                     "aligned_bindings": 0, "gram_joins": 0, "direct_source_updates_verified": 1}
            with experiment._operation():
                local_roots = experiment._roots(root)
                identity = (PROJECT, MISSION, "root", "context", current[3].identity, 20)
                value = experiment._get(local_roots["occurrences"], experiment._scope("occurrences"), identity)
            map_scope, map_digest = experiment._map_scope(identity[:-1]), value["map"]["digest"]
            items, expand = experiment._items, experiment._vector_positions

            def count_items(tree, scope, after=None):
                if scope == map_scope and tree["digest"] == map_digest:
                    reuse["map_walks"] += 1
                yield from items(tree, scope, after)

            def count_expansion(*args):
                reuse["vector_expansions"] += 1
                return expand(*args)

            with mock.patch.object(experiment, "_items", new=count_items):
                with experiment._operation():
                    first_map = experiment._map_values(value, identity[:-1])
                    self.assertIs(first_map, experiment._map_values(value, identity[:-1]))
                    forged = {**value, "map": {**value["map"], "count": value["map"]["count"] + 1}}
                    with self.assertRaisesRegex(IndexIntegrityError, "summary"):
                        experiment._map_values(forged, identity[:-1])
                    with self.assertRaisesRegex(IndexIntegrityError, "scope"):
                        experiment._map_values(value, (*identity[:-2], "other-owner"))
                self.assertEqual(reuse["map_walks"], 1)
                self.assertEqual((experiment._cache, experiment._pending), ({}, {}))
                del first_map
                with experiment._operation():
                    experiment._map_values(value, identity[:-1])
                self.assertEqual(reuse["map_walks"], 2)  # New request reauthenticates.
                # Deliberately LOWER the test admission limit: oversized
                # derived maps must not obtain an unaccounted side pool.
                with mock.patch.object(experiment, "cache_bytes_limit", 1), experiment._operation():
                    experiment._map_values(value, identity[:-1])
                    experiment._map_values(value, identity[:-1])
                    self.assertFalse(experiment._cache)
                self.assertEqual(reuse["map_walks"], 4)
                with experiment._operation(), mock.patch.object(experiment, "_vector_positions", new=count_expansion):
                    prepared = experiment._prepared_occurrence(identity, value)
                    current_inventory = experiment._inventory_at(local_roots, prepared, "current_at_cut")
                    experiment._inventory_at(local_roots, prepared, "retained_history")
                    logical = list(experiment._logical_scopes(prepared, ("current_at_cut", "retained_history")))
                    self.assertEqual(reuse["vector_expansions"], len(prepared.facts))
                    self.assertEqual(len(logical), 2 * len(prepared.facts))
                    other_scope_bytes = logical[1][1]._canonical_bytes()
                    logical[0][1].metadata["selected_at_commit"] = -999
                    self.assertEqual(logical[1][1]._canonical_bytes(), other_scope_bytes)
                    bad_inventory = (current_inventory[0], replace(current_inventory[1], metadata={
                        **current_inventory[1].metadata, "selected_at_commit": 999}))
                    changed_ordinary = pages_module.PageTree.replace_source(experiment,
                        local_roots["ordinary"], (current_inventory,), (bad_inventory,))
                    changed_scope = {**local_roots, "ordinary": changed_ordinary}
                    experiment._inventory_at(changed_scope, prepared, "retained_history")
                    with self.assertRaisesRegex(IndexIntegrityError, "qualifications"):
                        experiment._inventory_at(changed_scope, prepared, "current_at_cut")
                self.assertEqual(reuse["map_walks"], 5)
            resolve, join = experiment._resolved_occurrence, experiment._joined_occurrence

            def count_resolve(*args, **kwargs):
                reuse["aligned_bindings"] += 1
                return resolve(*args, **kwargs)

            def count_join(*args, **kwargs):
                reuse["gram_joins"] += 1
                return join(*args, **kwargs)

            with (mock.patch.object(experiment, "_resolved_occurrence", new=count_resolve),
                  mock.patch.object(experiment, "_joined_occurrence", new=count_join)):
                joined = tuple(experiment.search_field(root,
                    ("lexical", PROJECT, MISSION, "current_at_cut", "root", "context", "content"), "abcde"))
            self.assertEqual(reuse["aligned_bindings"], len(joined))
            self.assertEqual(reuse["gram_joins"], 3 * len(joined))
            self.assertEqual((experiment._cache, experiment._pending, experiment._depth), ({}, {}, 0))
            # No-op and rejected preimages preserve exact published
            # roots and the complete existing object table.
            same_delta = OwnerContentDelta(removed_current=(current[3],), removed_retained=(current[3],),
                                          added_current=(current[3],), added_retained=(current[3],))
            with mock.patch.object(experiment, "_roots", side_effect=AssertionError("invalid input read graph")):
                for invalid_delta, reason in (((), "OwnerContentDelta"),
                        (replace(same_delta, pending_capture_projection=True), "pending Capture"),
                        (OwnerContentDelta(added_current=(object(),)), "source projections"),
                        (OwnerContentDelta(removed_current=(current[3], current[3])), "duplicate"),
                        (OwnerContentDelta(added_current=(current[3], current[3])), "duplicate")):
                    with self.assertRaisesRegex(IndexIntegrityError, reason):
                        experiment.replace_source(root, invalid_delta)
            before_noop = _layout_totals(connection)
            same_root = experiment.replace_source(root, same_delta)
            self.assertEqual(same_root, root)
            self.assertEqual(_layout_totals(connection), before_noop)
            incomplete = replace(current[3], fields={variant: {**fields, "content": ""}
                for variant, fields in current[3].fields.items()})
            with self.assertRaisesRegex(IndexIntegrityError, "preimage"):
                experiment.replace_source(root, replace(same_delta, removed_current=(incomplete,)))
            bad = replace(current[3], payload_digest="f" * 64)
            with self.assertRaisesRegex(IndexIntegrityError, "collision"):
                experiment.replace_source(root, OwnerContentDelta(added_current=(bad,)))
            mismatched = replace(current[3], selected_at_commit=99)
            with self.assertRaisesRegex(IndexIntegrityError, "current/retained.*qualifications"):
                experiment.replace_source(root, OwnerContentDelta(removed_current=(current[3],), added_current=(mismatched,)))
            # Once the first update has happened, replaying its exact
            # old-current removal cannot silently select it again.
            with self.assertRaisesRegex(IndexIntegrityError, "preimage"):
                experiment.replace_source(root, deltas[-1][2])
            self.assertEqual(_layout_totals(connection), (retained_objects, retained_bytes))

            # Authenticated equivalent vector encodings are valid old
            # preimages. A stored bool masquerading as integer offset
            # is NOT equivalent, even where Python equality says so.
            for fault in ("absolute", "compact", "bool"):
                connection.execute("SAVEPOINT source_update_encoding")
                try:
                    with experiment._operation():
                        original_roots = experiment._roots(root)
                        owner = (PROJECT, MISSION, "root", "context", current[3].identity)
                        identity = owner + (current[3].revision,)
                        binding = experiment._get(original_roots["occurrences"], experiment._scope("occurrences"), identity)
                        facts = experiment._map_values(binding, owner)
                        key = ("id", "c") if fault == "bool" else ("content", "a")
                        scope = ("lexical", *owner[:2], "current_at_cut", *owner[2:4], *key)
                        positions = experiment._vector_positions(facts[key], scope, binding["lengths"][key[0]])
                        if fault == "compact":
                            runs = []
                            for before, after in pairwise(positions):
                                gap = after - before
                                if runs and runs[-1][0] == gap:
                                    runs[-1][1] += 1
                                else:
                                    runs.append([gap, 1])
                            alternate = {"first": positions[0], "count": len(positions), "gaps": runs}
                        else:
                            alternate = {"positions": list(positions)}
                            if fault == "bool":
                                self.assertEqual(positions[0], 0)
                                alternate["positions"][0] = False
                        changed_map = experiment._tree(experiment._map_scope(owner),
                            ((found, alternate if found == key else value) for found, value in facts.items()))
                        changed_binding = {**binding, "map": changed_map}
                        changed_occurrences = experiment._edit(original_roots["occurrences"], experiment._scope("occurrences"),
                            {identity: (binding, changed_binding)})
                        altered_root = experiment._commit(experiment._edit(root, experiment._ROOT,
                            {("occurrences",): (original_roots["occurrences"], changed_occurrences)}))
                    changes_before = connection.total_changes
                    if fault == "bool":
                        with self.assertRaisesRegex(IndexIntegrityError, "preimage.*positions"):
                            experiment.replace_source(altered_root, same_delta)
                        self.assertEqual(connection.total_changes, changes_before)
                    else:
                        canonical_root = experiment.replace_source(altered_root, same_delta)
                        assert_relation(experiment, canonical_root, expected[-1], qualified_sources(*cuts[-1]))
                        assert_relation(experiment, altered_root, expected[-1], qualified_sources(*cuts[-1]))
                    self.assertEqual((experiment._cache, experiment._pending, experiment._depth), ({}, {}, 0))
                finally:
                    connection.execute("ROLLBACK TO source_update_encoding")
                    connection.execute("RELEASE source_update_encoding")
            self.assertEqual(_layout_totals(connection), (retained_objects, retained_bytes))
            connection.execute("SAVEPOINT source_update_failure")
            try:
                stored_before = connection.total_changes
                class InterruptedUpdateConnection:
                    def __getattr__(self, name):
                        return getattr(connection, name)

                    def execute(self, statement, *args, **kwargs):
                        cursor = connection.execute(statement, *args, **kwargs)
                        if (statement.startswith("INSERT INTO packed_posting_experiment ")
                                and connection.total_changes > stored_before):
                            raise RuntimeError("injected source-update persistence failure")
                        return cursor

                failed_source = source(3, 41, current[3].fields["root"]["content"] + " failure sentinel")
                with mock.patch.object(packed, "connection", new=InterruptedUpdateConnection()):
                    with self.assertRaisesRegex(RuntimeError, "source-update persistence"):
                        experiment.replace_source(root, OwnerContentDelta(removed_current=(current[3],),
                            added_current=(failed_source,), added_retained=(failed_source,)))
                self.assertGreater(connection.total_changes, stored_before)
                self.assertTrue(connection.in_transaction)
                self.assertEqual((experiment._cache, experiment._pending, experiment._depth), ({}, {}, 0))
            finally:
                connection.execute("ROLLBACK TO source_update_failure")
                connection.execute("RELEASE source_update_failure")
            self.assertEqual(_layout_totals(connection), (retained_objects, retained_bytes))

            # Metadata-only replacement at an existing retained key:
            # the exact qualifications change, but field maps and
            # presence events are byte-identical. Then insert revision
            # 9 out of order, proving actual gapped inventory handling.
            selected = replace(initial[3], selected_at_commit=23, retired_at_commit=24)
            metadata_root = experiment.replace_source(root,
                OwnerContentDelta(removed_retained=(initial[3],), added_retained=(selected,)))
            with experiment._operation():
                before_roots, after_roots = experiment._roots(root), experiment._roots(metadata_root)
                self.assertEqual(before_roots["events"], after_roots["events"])
                self.assertEqual(before_roots["current"], after_roots["current"])
                owner = (PROJECT, MISSION, "root", "context", initial[3].identity)
                binding_before = experiment._get(before_roots["occurrences"], experiment._scope("occurrences"), owner + (2,))
                binding_after = experiment._get(after_roots["occurrences"], experiment._scope("occurrences"), owner + (2,))
                self.assertEqual(binding_before["map"], binding_after["map"])
                self.assertNotEqual(binding_before["source"], binding_after["source"])
            adjusted = tuple(selected if item == initial[3] else item for item in retained)
            assert_relation(experiment, metadata_root, all_entries(current, adjusted), qualified_sources(current, adjusted))
            middle = source(3, 9, "abcde qzx wrong positional abc--bcd--cde")
            middle_root = experiment.replace_source(metadata_root, OwnerContentDelta(added_retained=(middle,)))
            with experiment._operation():
                old_events = experiment._roots(metadata_root)["events"]
                new_events = experiment._roots(middle_root)["events"]
                boundary = (PROJECT, MISSION, "root", "context", "content", "bx", middle.identity)
                self.assertEqual(experiment._get(old_events, experiment._scope("events"), boundary + (11,)),
                                 {"present": False})
                self.assertEqual(experiment._get(new_events, experiment._scope("events"), boundary + (9,)),
                                 {"present": False})
                self.assertIsNone(experiment._get(new_events, experiment._scope("events"), boundary + (11,)))
            middle_cut = (tuple(current), adjusted + (middle,))
            assert_relation(experiment, middle_root, all_entries(*middle_cut), qualified_sources(*middle_cut))
            query(experiment, middle_root, middle_cut, "retained_history", "root", "abcde")
            prefix = ("lexical", PROJECT, MISSION, "retained_history", "root", "context", "content")
            # The positive 'a' interval starts at revision 2. Resuming
            # inside it MUST find actual revision 9, not skip to a later
            # presence event or invent revision 8.
            page, _more = _OccurrenceSearchFixture(experiment, middle_root).page(
                prefix, "a", 1, after=("context", initial[3].identity, 7))
            self.assertEqual(page[0].key, ("context", initial[3].identity, 9))
            # A pure positional decoy contains every trigram but not
            # the substring. This uses source-owned expected strings.
            decoy = source(4, 3, "abc--bcd--cde")
            decoy_root = experiment.replace_source(middle_root, OwnerContentDelta(added_retained=(decoy,)))
            decoy_cut = (tuple(current), middle_cut[1] + (decoy,))
            query(experiment, decoy_root, decoy_cut, "retained_history", "root", "abcde")
            assert_relation(experiment, decoy_root, all_entries(*decoy_cut), qualified_sources(*decoy_cut))

            # An inventory-only occurrence is a real absence boundary,
            # not a missing source. Reappearance and removal of an
            # interior retained occurrence repair both neighboring
            # event transitions without assuming append-only history.
            empty_document = {"known_omissions": []}
            empty = SourceProjection(PROJECT, MISSION, "context", current[3].identity, 25,
                hashlib.sha256(canonical_json_bytes(empty_document)).hexdigest(), 25, empty_document,
                {variant: {field: "" for field in index_module.SEARCH_FIELDS}
                 for variant in ("root", "delegated")}, selected_at_commit=25)
            empty_root = experiment.replace_source(root, OwnerContentDelta(removed_current=(current[3],),
                added_current=(empty,), added_retained=(empty,)))
            empty_current = tuple(empty if item == current[3] else item for item in current)
            assert_relation(experiment, empty_root, all_entries(empty_current, (*retained, empty)),
                            qualified_sources(empty_current, (*retained, empty)))
            restored = source(3, 31, "abcde qzx")
            restored_root = experiment.replace_source(empty_root, OwnerContentDelta(removed_current=(empty,),
                added_current=(restored,), added_retained=(restored,)))
            restored_current = tuple(restored if item == empty else item for item in empty_current)
            restored_cut = (restored_current, (*retained, empty, restored))
            assert_relation(experiment, restored_root, all_entries(*restored_cut), qualified_sources(*restored_cut))
            query(experiment, restored_root, restored_cut, "retained_history", "root", "abcde")
            removed_middle_root = experiment.replace_source(middle_root, OwnerContentDelta(removed_retained=(middle,)))
            assert_relation(experiment, removed_middle_root, all_entries(current, adjusted), qualified_sources(current, adjusted))
            retired = replace(restored, retired_at_commit=35)
            retired_root = experiment.replace_source(restored_root, OwnerContentDelta(
                removed_current=(restored,), removed_retained=(restored,), added_retained=(retired,)))
            retired_cut = (tuple(item for item in restored_current if item != restored), (*retained, empty, retired))
            assert_relation(experiment, retired_root, all_entries(*retired_cut), qualified_sources(*retired_cut))
            query(experiment, retired_root, retired_cut, "current_at_cut", "root", "qzx")

            original_items, original_node = experiment._items, experiment._node
            seek_calls = {"current": 0, "events": 0, "occurrences": 0}
            original_seek = experiment._seek

            def counted_seek(tree, scope, key, **kwargs):
                if scope in tuple(experiment._scope(name) for name in seek_calls):
                    seek_calls[scope[-1]] += 1
                return original_seek(tree, scope, key, **kwargs)

            def no_scan(tree, scope, after=None):
                if scope in tuple(experiment._scope(name) for name in seek_calls):
                    raise AssertionError("query restarted a full candidate inventory iterator")
                yield from original_items(tree, scope, after)

            def current_only(tree, scope, **kwargs):
                if scope == experiment._scope("events"):
                    raise AssertionError("current query read retained presence events")
                return original_node(tree, scope, **kwargs)

            with (mock.patch.object(experiment, "_items", new=no_scan),
                  mock.patch.object(experiment, "_seek", new=counted_seek),
                  mock.patch.object(experiment, "_node", new=current_only)):
                query(experiment, root, cuts[-1], "current_at_cut", "root", "qzx")
            self.assertEqual(seek_calls["events"], 0)
            with (mock.patch.object(experiment, "_items", new=no_scan),
                  mock.patch.object(experiment, "_seek", new=counted_seek)):
                query(experiment, middle_root, middle_cut, "retained_history", "root", "qzx")
            self.assertGreater(seek_calls["events"], 0)

            # Capture's delegated occurrence 0 and root occurrence 3
            # coexist. Root 3->9 must not change delegated head/source.
            capture = {"capture_id": "capture.occurrence", "project_id": PROJECT, "mission_id": MISSION,
                "executive_epoch_id": "epoch.one", "capture_kind": "output", "observation_id": "observation.one",
                "assignment_id": "assignment.one", "provenance": {}, "completion": {"state": "completed"}, "artifacts": []}
            descriptor = {"id": "capture:capture.occurrence", "kind": "capture", "title": "capture.occurrence",
                "capture_kind": "output", "origin_epoch_id": "epoch.one", "origin_epoch_state": "completed",
                "late_classification": None, "cut_relation": "after_latest_checkpoint", "pending_state": "current_pending",
                "native_lineage": None, "artifacts": [], "failed_interval_or_late_output": False,
                "trust_class": "custody_descriptor", "completeness": "descriptor"}
            captures = capture_source_projections(project_id=PROJECT, capture=capture, origin_commit=2,
                root_descriptor=descriptor, projection_commit=3)
            capture_root = experiment.replace_source(root, OwnerContentDelta(added_current=captures, added_retained=captures))
            newer = capture_source_projections(project_id=PROJECT, capture=capture, origin_commit=2,
                root_descriptor={**descriptor, "pending_state": "fully_covered"}, projection_commit=9)[1]
            capture_changed = experiment.replace_source(capture_root, OwnerContentDelta(
                removed_current=(captures[1],), added_current=(newer,), added_retained=(newer,)))
            for variant, revision in (("delegated", 0), ("root", 9)):
                directory = ("lexical", PROJECT, MISSION, "current_at_cut", variant, "capture", "id")
                page, _more = _OccurrenceSearchFixture(experiment, capture_changed).page(directory, "capture", 1)
                self.assertEqual(page[0].key, ("capture", capture["capture_id"], revision))
                self.assertEqual(page[0].source_origin_commit, 2)
                self.assertEqual(page[0].origin_commit, 2 if variant == "delegated" else 9)
                self.assertEqual(page[0].metadata["reference"]["revision"], 0)
            assert_relation(experiment, capture_root, expected[-1] + all_entries(captures, captures),
                            qualified_sources(*cuts[-1]) + qualified_sources(captures, captures))
            assert_relation(experiment, capture_changed, expected[-1] + all_entries((captures[0], newer), (*captures, newer)),
                            qualified_sources(*cuts[-1]) + qualified_sources((captures[0], newer), (*captures, newer)))
            capture_inventory = qualified_sources((captures[0], newer), (*captures, newer))
            capture_entries = all_entries((captures[0], newer), (*captures, newer))
            bad_capture = replace(newer, inventory_metadata={"capture_descriptor": {
                **newer.inventory_metadata["capture_descriptor"], "pending_state": "current_pending"}})
            capture_extent = _layout_totals(connection)
            with self.assertRaisesRegex(IndexIntegrityError, "preimage.*qualifications"):
                experiment.replace_source(capture_changed, OwnerContentDelta(removed_current=(bad_capture,)))
            self.assertEqual(_layout_totals(connection), capture_extent)
            capture_bulk = source_build(reversed(capture_inventory))
            self.assertEqual(experiment.bootstrap_buffers["logical_memberships"], len(capture_entries))
            assert_relation(experiment, capture_bulk, capture_entries, capture_inventory)
            wrong_descriptor = replace(newer, inventory_metadata={"capture_descriptor":
                {**newer.inventory_metadata["capture_descriptor"], "pending_state": "current_pending"}})
            with self.assertRaisesRegex(IndexIntegrityError, "ordinary relation"):
                experiment.audit_sources(capture_changed, qualified_sources(*cuts[-1]) +
                    qualified_sources((captures[0], wrong_descriptor), (*captures, wrong_descriptor)))
            connection.execute("SAVEPOINT bootstrap_capture_collision")
            try:
                source_build((("current_at_cut", newer), ("current_at_cut", wrong_descriptor)),
                             error="membership collision")
            finally:
                connection.execute("ROLLBACK TO bootstrap_capture_collision")
                connection.execute("RELEASE bootstrap_capture_collision")
            bootstrap_buffers["capture_source_qualifications_verified"] = 1

            # Missing/corrupt routed descendants must fail on the NEXT
            # request, after the previous operation cache was cleared.
            with experiment._operation():
                bound_roots = experiment._roots(root)
                binding = experiment._get(bound_roots["occurrences"], experiment._scope("occurrences"),
                    (PROJECT, MISSION, "root", "context", current[3].identity, 20))
            directory = ("lexical", PROJECT, MISSION, "current_at_cut", "root", "context", "content", "qzx")
            for digest in (binding["map"]["digest"], binding["source"], bound_roots["current"]["digest"]):
                for damage in ("missing", "changed"):
                    connection.execute("SAVEPOINT occurrence_damage")
                    try:
                        if damage == "missing":
                            connection.execute("DELETE FROM packed_posting_experiment WHERE digest=?", (digest,))
                        else:
                            connection.execute("UPDATE packed_posting_experiment SET body='{}' WHERE digest=?", (digest,))
                        with self.assertRaises(IndexIntegrityError):
                            experiment.posting(root, directory, ("context", current[3].identity, 20))
                        with self.assertRaises(IndexIntegrityError):
                            experiment.audit_sources(root, qualified_sources(*cuts[-1]))
                        self.assertEqual((experiment._cache, experiment._pending, experiment._depth), ({}, {}, 0))
                    finally:
                        connection.execute("ROLLBACK TO occurrence_damage")
                        connection.execute("RELEASE occurrence_damage")
            # A rehashed omitted inverse event can be internally
            # authenticated. Independent map/inverse audit, not a false
            # negative-query guarantee, is what must reject omission.
            connection.execute("SAVEPOINT missing_event")
            try:
                with experiment._operation():
                    event_key = (PROJECT, MISSION, "root", "context", "content", "qzx", current[3].identity, 11)
                    event_value = experiment._get(bound_roots["events"], experiment._scope("events"), event_key)
                    self.assertIsNotNone(event_value)
                    bad_events = experiment._edit(bound_roots["events"], experiment._scope("events"), {event_key: (event_value, None)})
                    broken = experiment._commit(experiment._edit(root, experiment._ROOT,
                        {("events",): (bound_roots["events"], bad_events)}))
                with self.assertRaisesRegex(IndexIntegrityError, "inverse/field-map"):
                    tuple(experiment.iter_entries(broken))
                with self.assertRaisesRegex(IndexIntegrityError, "inverse/field-map"):
                    experiment.audit_sources(broken, qualified_sources(*cuts[-1]))
            finally:
                connection.execute("ROLLBACK TO missing_event")
                connection.execute("RELEASE missing_event")
            connection.execute("SAVEPOINT missing_event_anchor")
            try:
                with experiment._operation():
                    anchor_identity = (PROJECT, MISSION, "root", "context", current[7].identity, 2)
                    anchor = experiment._get(bound_roots["occurrences"], experiment._scope("occurrences"), anchor_identity)
                    absent = experiment._edit(bound_roots["occurrences"], experiment._scope("occurrences"),
                        {anchor_identity: (anchor, None)})
                    broken = experiment._commit(experiment._edit(root, experiment._ROOT,
                        {("occurrences",): (bound_roots["occurrences"], absent)}))
                with self.assertRaisesRegex(IndexIntegrityError, "positive event occurrence"):
                    tuple(experiment.search_field(broken, guarded_prefix, "qzx",
                        after=("context", current[7].identity, 0)))
                with self.assertRaisesRegex(IndexIntegrityError, "inventory.*relation"):
                    experiment.audit_sources(broken, qualified_sources(*cuts[-1]))
                self.assertEqual((experiment._cache, experiment._pending, experiment._depth), ({}, {}, 0))
            finally:
                connection.execute("ROLLBACK TO missing_event_anchor")
                connection.execute("RELEASE missing_event_anchor")
            self.assertEqual(_OccurrenceSearchFixture(experiment, root).page(
                guarded_prefix, "qzx", 1, after=("context", current[7].identity, 2)), ((), False))
            # Outward generator cancellation also releases all proof
            # state; the next request must authenticate afresh.
            iterator = experiment.postings(root, directory)
            self.assertIsNotNone(next(iterator))
            iterator.close()
            self.assertEqual((experiment._cache, experiment._pending, experiment._depth), ({}, {}, 0))
            self.assertLessEqual(experiment.peak_cache_items, experiment.cache_items_limit)
            self.assertLessEqual(experiment.peak_cache_bytes, experiment.cache_bytes_limit)

    def setUp(self):
        self.connection = sqlite3.connect(":memory:")
        self.addCleanup(self.connection.close)
        self.connection.execute(
            "CREATE TABLE owner_content_index_node ("
            "node_digest TEXT PRIMARY KEY, node_kind TEXT, node_json TEXT)"
        )

    def _view(self, descriptor=None):
        return index_module.OwnerContentIndex(
            self.connection, PROFILE, descriptor, integrity_error=IndexIntegrityError,
        )

    def _entries(
        self, owner_id, content, *, revision=1, origin=None,
        scope="retained_history", kind="evidence", variant="root", fields=None,
    ):
        reference = f"{kind}:{owner_id}@{revision}"
        return tuple(index_module.entries_for_fields(
            project_id=PROJECT, mission_id=MISSION, revision_scope=scope,
            variant=variant, kind=kind, owner_id=owner_id, revision=revision,
            reference=reference, payload_digest=hashlib.sha256(reference.encode()).hexdigest(),
            origin_commit=revision if origin is None else origin,
            fields={"content": content} if fields is None else fields,
        ))

    def _prefix(self, *, scope="retained_history", kind="evidence", field="content", variant="root"):
        return ("lexical", PROJECT, MISSION, scope, variant, kind, field)

    def _source(self, owner_id, content, *, revision=1, origin=None,
                kind="evidence", variant="root", fields=None, document=None):
        from research_core.owner_content_access import SourceProjection
        variants = (variant,) if isinstance(variant, str) else variant
        values = {"content": content} if fields is None else fields
        projections = {name: {field: values.get(field, "")
                              for field in index_module.PROJECTION_FIELDS[name][kind]}
                       for name in variants}
        document = {} if document is None else document
        digest = hashlib.sha256(canonical_json_bytes({
            "identity": owner_id, "revision": revision, "fields": projections,
            "document": document,
        })).hexdigest()
        return SourceProjection(PROJECT, MISSION, kind, owner_id, revision,
                                digest, revision if origin is None else origin,
                                document, projections)

    def _publish(self, inventory, descriptor=None):
        from research_core.owner_content_access import OwnerContentDelta
        inventory = tuple(inventory)
        self.connection.execute("BEGIN")
        index = self._view(descriptor)
        if descriptor is None:
            result = index.bootstrap(iter(inventory))
        else:
            result = index.apply_delta(OwnerContentDelta(
                added_current=tuple(source for scope, source in inventory if scope == "current_at_cut"),
                added_retained=tuple(source for scope, source in inventory if scope == "retained_history"),
            ))
        self.connection.commit()
        return result

    def _audit(self, descriptor, inventory):
        from research_core.owner_content_access import source_entries
        inventory = tuple(inventory)
        expected = {(directory, posting.key): posting.to_mapping()
                    for scope, source in inventory for directory, posting in source_entries(source, scope)}
        view = self._view(descriptor)
        self.assertEqual(view.audit_sources(iter(inventory)), len(expected))
        self.assertEqual({(directory, posting.key): posting.to_mapping()
                          for directory, posting in view.iter_entries()}, expected)

    def _keys(self, descriptor, query, **prefix_options):
        return tuple(item.key for item in self._view(descriptor).search_field(
            directory_prefix=self._prefix(**prefix_options), query=query,
        ))

    def _rows(self):
        return dict((row[0], tuple(row[1:])) for row in self.connection.execute(
            "SELECT node_digest, node_kind, node_json FROM owner_content_index_node"
        ))

    def _body(self, digest):
        row = self.connection.execute(
            "SELECT node_json FROM owner_content_index_node WHERE node_digest = ?", (digest,),
        ).fetchone()
        self.assertIsNotNone(row)
        return json.loads(row[0])

    def _seal_fixture_node(self, body):
        """Rehash a test corruption so semantic validation, not just hashing, is exercised."""
        digest = index_module._hash(body)
        self.connection.execute(
            "INSERT OR REPLACE INTO owner_content_index_node VALUES (?, ?, ?)",
            (digest, body["kind"], canonical_json_bytes(body).decode("utf-8")),
        )
        return digest

    def test_current_replacement_preserves_retained_memberships_and_every_old_root(self):
        from research_core.owner_content_access import OwnerContentDelta
        identities = ("evidence.program", "evidence.sibling", "evidence.retired", "evidence.history")
        old = tuple(self._source(identity, "old lemma", variant=("root", "delegated"))
                    for identity in identities)
        old_inventory = tuple((scope, source) for scope in ("retained_history", "current_at_cut")
                              for source in old)
        old_descriptor = self._publish(old_inventory)
        old_rows = self._rows()
        new = tuple(self._source(identity, "new lemma", revision=2, variant=("root", "delegated"))
                    for identity in (identities[0], identities[1], identities[3]))
        # Replace two heads, clear one without deleting its history, and add
        # retained-only history to another owner whose current head survives.
        delta = OwnerContentDelta(removed_current=old[:3], added_retained=new, added_current=new[:2])
        self.connection.execute("BEGIN")
        plain_descriptor = self._view(old_descriptor).apply_delta(delta)
        plain_rows = self._rows()
        self.connection.rollback()
        self.assertEqual(self._rows(), old_rows)

        view = self._view(old_descriptor)
        engine = view._engine
        original_groups, original_get = engine._source_groups, engine._get
        active_head_owner, get_depth, observed_heads = None, 0, set()

        class ObservedIdentity(tuple):
            def __getitem__(identity, key):
                value = super().__getitem__(key)
                if key == slice(None, -1) and active_head_owner is not None:
                    self.assertEqual(value, active_head_owner,
                                     "head selection rescanned another owner's occurrence")
                return value

        def observed_groups(inventory):
            groups, ordinary = original_groups(inventory)
            return {(ObservedIdentity(identity), selected): group
                    for (identity, selected), group in groups.items()}, ordinary

        def observed_get(tree, scope, key):
            nonlocal active_head_owner, get_depth
            outer = get_depth == 0
            if outer:
                active_head_owner = None
            get_depth += 1
            try:
                value = original_get(tree, scope, key)
            finally:
                get_depth -= 1
            if outer and scope == engine._scope("heads"):
                active_head_owner = tuple(key)
                observed_heads.add(active_head_owner)
            return value

        # Observe only candidate selection between the real head lookup and
        # its next occurrence lookup. Every affected owner has an old head,
        # so removal also closes that interval. Ordinary tuple meaning and
        # source/index work remain unchanged; the former all-binding scan
        # fails on its first foreign owner without a timing or count limit.
        self.connection.execute("BEGIN")
        with (mock.patch.object(engine, "_source_groups", new=observed_groups),
              mock.patch.object(engine, "_get", new=observed_get)):
            new_descriptor = view.apply_delta(delta)
        self.assertEqual(get_depth, 0)
        self.assertEqual(observed_heads, {(PROJECT, MISSION, variant, "evidence", identity)
                                         for variant in ("root", "delegated") for identity in identities})
        self.assertEqual(new_descriptor, plain_descriptor)
        self.assertEqual(self._rows(), plain_rows)
        self.connection.commit()
        self.assertNotEqual(old_descriptor, new_descriptor)
        for variant in ("root", "delegated"):
            self.assertEqual(self._keys(old_descriptor, "old", scope="current_at_cut", variant=variant),
                             tuple(("evidence", identity, 1) for identity in sorted(identities)))
            self.assertEqual(self._keys(new_descriptor, "old", scope="current_at_cut", variant=variant),
                             (("evidence", identities[3], 1),))
            self.assertEqual(self._keys(new_descriptor, "new", scope="current_at_cut", variant=variant),
                             tuple(("evidence", source.identity, 2) for source in new[:2]))
            self.assertEqual(self._keys(new_descriptor, "lemma", variant=variant),
                             tuple(sorted(("evidence", source.identity, source.revision) for source in (*old, *new))))
        self.assertTrue(all(self._rows()[digest] == row for digest, row in old_rows.items()))
        self._audit(old_descriptor, old_inventory)
        self._audit(new_descriptor, tuple(("retained_history", source) for source in (*old, *new))
                    + tuple(("current_at_cut", source) for source in (*new[:2], old[3])))
        # Conflicting incoming revisions must still be checked after clearing
        # the old head, not silently collapsed by the per-owner grouping.
        conflicts = tuple(self._source(identities[0], "conflicting lemma", revision=revision,
                                       variant=("root", "delegated")) for revision in (3, 4))
        self.connection.execute("BEGIN")
        with self.assertRaisesRegex(IndexIntegrityError, "owner has multiple current heads"):
            self._view(new_descriptor).apply_delta(OwnerContentDelta(
                removed_current=(new[0],), added_current=conflicts, added_retained=conflicts))
        self.assertEqual(self._rows(), plain_rows)
        self.connection.rollback()

    def test_positional_intersection_matches_literal_casefolded_substrings(self):
        bodies = {
            "evidence.contiguous": "abcde", "evidence.unordered": "abc--bcd--cde",
            "evidence.short-repeat": "aaa", "evidence.long-repeat": "aaaaa",
            "evidence.expanding-casefold": "Straße", "evidence.composed": "café",
            "evidence.decomposed": "cafe\u0301",
        }
        inventory = tuple(("retained_history", self._source(owner, body)) for owner, body in bodies.items())
        descriptor = self._publish(inventory)
        for query in ("abcde", "aaaa", "A", "aa", "SS", "STRASSE", "é", "e\u0301", "absent"):
            with self.subTest(query=query):
                expected = tuple(("evidence", owner, 1) for owner in sorted(bodies)
                                 if query.casefold() in bodies[owner].casefold())
                self.assertEqual(self._keys(descriptor, query), expected)
        directory = index_module.lexical_directory_key(
            PROJECT, MISSION, "retained_history", "root", "evidence", "content", "ss")
        folded = self._view(descriptor).posting(directory, ("evidence", "evidence.expanding-casefold", 1))
        self.assertEqual(folded.positions, (4,))
        self.assertEqual(folded.field_length, len("strasse"))
        self._audit(descriptor, inventory)

    def test_complete_graph_audit_can_be_compared_with_independent_field_rederivation(self):
        documents = {
            "evidence.first": {"title": "Wide", "content": "abaß"},
            "evidence.second": {"title": "", "content": "ba"},
        }
        inventory = tuple(("retained_history", self._source(owner, "", fields=fields))
                          for owner, fields in documents.items())
        descriptor = self._publish(inventory)
        expected = {}
        for owner, fields in documents.items():
            for field_name, original in fields.items():
                text = original.casefold()
                grams = {text[start:end] for start in range(len(text))
                         for end in range(start + 1, min(len(text), start + 3) + 1)}
                for gram in grams:
                    key = self._prefix(field=field_name) + (gram,)
                    positions = tuple(pos for pos in range(len(text)) if text.startswith(gram, pos))
                    expected[(key, ("evidence", owner, 1))] = (positions, len(text))
        audit = self._view(descriptor)
        observed = {(directory, posting.key): (posting.positions, posting.field_length)
                    for directory, posting in audit.iter_entries() if directory[0] == "lexical"}
        self.assertEqual(observed, expected)
        self.assertEqual(audit.audit_sources(iter(inventory)), len(expected) + len(documents))
        self.assertEqual(descriptor["contract_version"], 2)
        self.assertEqual(descriptor["root"]["count"], 5)  # Manifest roles, not logical postings.
        altered = replace(inventory[0][1], fields={"root": {
            **inventory[0][1].fields["root"], "content": "different qualified source"}})
        with self.assertRaises(IndexIntegrityError):
            audit.audit_sources(iter((("retained_history", altered), inventory[1])))

    def test_absent_directory_and_absent_posting_use_authenticated_paths(self):
        descriptor = self._publish((("retained_history", self._source("evidence.first", "needle")),))
        directory = self._prefix() + ("nee",)
        for action in (
            lambda view: tuple(view.search_field(directory_prefix=self._prefix(), query="zzz")),
            lambda view: view.posting(directory, ("evidence", "evidence.absent", 1)),
        ):
            with self.subTest(action=action):
                view = self._view(descriptor)
                self.assertFalse(action(view))
                self.assertGreater(view.node_reads, 0)
        self.connection.execute(
            "DELETE FROM owner_content_index_node WHERE node_digest = ?", (descriptor["root"]["digest"],))
        with self.assertRaisesRegex(IndexIntegrityError, "absent"):
            tuple(self._view(descriptor).search_field(directory_prefix=self._prefix(), query="zzz"))

    def _compact_relation_body(self, descriptor, name):
        view = self._view(descriptor)
        with view.operation():
            root = view._engine._roots(descriptor["root"])[name]
        return self._body(root["digest"])

    def _compact_reseal_page(self, body):
        """Rehash physical corruption with its byte extent, not stale-digest rejection."""
        field = "entries" if body["kind"] == "range-cell" else "children"
        body["used_bytes"] = len(canonical_json_bytes({
            "key_prefix": body["key_prefix"], field: body[field],
        }))
        return {"digest": self._seal_fixture_node(body), **{
            name: body[name] for name in ("kind", "count", "first", "last", "height", "used_bytes")
        }}

    def _compact_replace_relation(self, descriptor, name, summary):
        manifest = self._body(descriptor["root"]["digest"])
        self.assertEqual(manifest["kind"], "range-cell")
        selected = [row for row in manifest["entries"]
                    if tuple(manifest["key_prefix"] + row[0]) == (name,)]
        self.assertEqual(len(selected), 1)
        self.assertEqual(set(selected[0][1]), {"inline"})
        selected[0][1]["inline"] = summary
        return {**descriptor, "root": self._compact_reseal_page(manifest)}

    def _compact_replace_single_map(self, descriptor, map_body):
        occurrences = self._compact_relation_body(descriptor, "occurrences")
        self.assertEqual(occurrences["kind"], "range-cell")
        self.assertEqual(len(occurrences["entries"]), 1)
        binding = occurrences["entries"][0][1]["inline"]
        binding["map"] = self._compact_reseal_page(map_body)
        return self._compact_replace_relation(descriptor, "occurrences", self._compact_reseal_page(occurrences))

    def test_missing_posting_node_is_not_a_negative_search_result(self):
        descriptor = self._publish((("retained_history", self._source("evidence.first", "a")),))
        occurrence = self._compact_relation_body(descriptor, "occurrences")
        map_digest = occurrence["entries"][0][1]["inline"]["map"]["digest"]
        events = self._compact_relation_body(descriptor, "events")
        for digest, key in (
            (map_digest, ("evidence", "evidence.first", 1)),
            (index_module._hash(events), ("evidence", "missing", 1)),
        ):
            with self.subTest(missing=digest):
                self.connection.execute(
                    "DELETE FROM owner_content_index_node WHERE node_digest = ?", (digest,),
                )
                with self.assertRaisesRegex(IndexIntegrityError, "referenced object is absent"):
                    self._view(descriptor).posting(self._prefix() + ("a",), key)
                with self.assertRaisesRegex(IndexIntegrityError, "referenced object is absent"):
                    tuple(self._view(descriptor).search_field(directory_prefix=self._prefix(), query="a"))
                self.connection.rollback()

    def test_digest_mismatch_and_closed_descriptor_mismatch_reject(self):
        descriptor = self._publish((("retained_history", self._source("evidence.first", "a")),))
        for changes in (
            {"root": {**descriptor["root"], "count": descriptor["root"]["count"] + 1}},
            {"root": {**descriptor["root"], "used_bytes": descriptor["root"]["used_bytes"] + 1}},
            {"root": {**descriptor["root"], "extra": True}},
            {"projection_profile_digest": "f" * 64},
            {"contract_version": 1},
            {"contract_version": True},
            {"additional_field": "not authorized"},
        ):
            with self.subTest(changes=changes), self.assertRaises(IndexIntegrityError):
                self._view({**descriptor, **changes})

        digest = descriptor["root"]["digest"]
        body = self._body(digest)
        canonical = canonical_json_bytes(body).decode("utf-8")
        self.assertEqual(index_module._hash(body), digest)
        # The same decoded meaning and digest cannot authorize noncanonical
        # bytes; strict decoding also still rejects duplicate/nonfinite JSON.
        for encoded in (
            json.dumps(body, indent=2, ensure_ascii=True),
            canonical[:-1] + ',"count":5}',
            canonical.replace('"count":5', '"count":NaN', 1),
        ):
            with self.subTest(encoded=encoded):
                self.connection.execute(
                    "UPDATE owner_content_index_node SET node_json = ? WHERE node_digest = ?",
                    (encoded, digest),
                )
                with self.assertRaises(IndexIntegrityError):
                    self._view(descriptor)
        for changed in ({**body, "domain": "wrong"}, {**body, "scope": ["wrong"]}):
            self.connection.execute(
                "UPDATE owner_content_index_node SET node_json = ? WHERE node_digest = ?",
                (canonical_json_bytes(changed).decode("utf-8"), digest),
            )
            with self.assertRaisesRegex(IndexIntegrityError, "object authentication failed"):
                self._view(descriptor)
        body["count"] += 1
        self.connection.execute(
            "UPDATE owner_content_index_node SET node_json = ? WHERE node_digest = ?",
            (canonical_json_bytes(body).decode("utf-8"), digest),
        )
        with self.assertRaisesRegex(IndexIntegrityError, "object authentication failed"):
            self._view(descriptor)

    def test_rehashed_bad_positions_and_counts_are_rejected(self):
        descriptor = self._publish((("retained_history", self._source("evidence.first", "a")),))
        directory = self._prefix() + ("a",)
        posting = self._view(descriptor).posting(directory, ("evidence", "evidence.first", 1))
        self.assertIsNotNone(posting)
        occurrence = self._compact_relation_body(descriptor, "occurrences")
        map_digest = occurrence["entries"][0][1]["inline"]["map"]["digest"]
        # Validation-only maintenance and returned decoding keep the same
        # closed-shape, identity, then lexical-bound rejection order.
        for changes, message in (
            ({"extra": True, "positions": [-1]}, "owner content posting value has an unknown shape"),
            ({"origin_commit": True, "positions": [1]}, "owner content posting identity, origin or positions are invalid"),
            ({"positions": [1]}, "owner content positions exceed the casefolded field"),
        ):
            for validate in (index_module._validate_posting_value, index_module._posting_from_value):
                with self.subTest(changes=changes, validate=validate.__name__):
                    with self.assertRaisesRegex(ValueError, "^" + message + "$"):
                        validate(posting.key, {**posting.to_mapping(), **changes}, directory)
        corruptions = (
            ("negative position", {"positions": [-1]}, None, IndexIntegrityError, "identity, origin or positions are invalid"),
            ("duplicate position", {"positions": [0, 0]}, None, IndexIntegrityError, "identity, origin or positions are invalid"),
            ("outside field", {"positions": [1]}, None, IndexIntegrityError, "positions exceed the casefolded field"),
            ("bool position", {"positions": [False]}, None, IndexIntegrityError, "identity, origin or positions are invalid"),
            ("unknown position shape", {"positions": [0], "extra": True}, None, IndexIntegrityError, "position vector payload differs"),
            ("false page count", None, {"count": 2}, IndexIntegrityError, "cell order/count differs"),
            ("false page height", None, {"height": 2}, IndexIntegrityError, "cell order/count differs"),
        )
        for name, payload, summary, error_type, message in corruptions:
            with self.subTest(corruption=name):
                body = self._body(map_digest)
                self.assertEqual(body["kind"], "range-cell")
                self.assertEqual(len(body["entries"]), 1)
                if payload is not None:
                    body["entries"][0][1] = {"inline": payload}
                if summary is not None:
                    body.update(summary)
                forged = self._compact_replace_single_map(descriptor, body)
                with self.assertRaisesRegex(error_type, message):
                    tuple(self._view(forged).search_field(directory_prefix=self._prefix(), query="a"))
                self.connection.rollback()

        occurrence = self._compact_relation_body(descriptor, "occurrences")
        occurrence["entries"][0][1]["inline"]["map"]["count"] += 1
        forged = self._compact_replace_relation(descriptor, "occurrences", self._compact_reseal_page(occurrence))
        with self.assertRaisesRegex(IndexIntegrityError, "scope/summary differs"):
            tuple(self._view(forged).search_field(directory_prefix=self._prefix(), query="a"))

    def test_rehashed_posting_order_and_child_summary_are_rejected(self):
        descriptor = self._publish(
            ("retained_history", self._source(f"evidence.{ordinal:03d}", "a")) for ordinal in range(32)
        )
        for corruption, message in (
            ("overlapping child keys", "branch range/count differs"),
            ("unordered occurrence keys", "cell order/count differs"),
            ("false child count", "scope/summary differs"),
        ):
            with self.subTest(corruption=corruption):
                body = self._compact_relation_body(descriptor, "occurrences")
                self.assertEqual(body["kind"], "range-branch")
                self.assertGreaterEqual(len(body["children"]), 2)
                # Compact child order is digest/kind/count/first/last/height/used_bytes.
                if corruption == "overlapping child keys":
                    body["children"][1][3] = list(body["children"][0][4])
                elif corruption == "unordered occurrence keys":
                    child = self._body(body["children"][0][0])
                    self.assertEqual(child["kind"], "range-cell")
                    self.assertGreaterEqual(len(child["entries"]), 4)
                    child["entries"][1], child["entries"][2] = child["entries"][2], child["entries"][1]
                    replacement = self._compact_reseal_page(child)
                    body["children"][0][0] = replacement["digest"]
                    body["children"][0][6] = replacement["used_bytes"]
                else:
                    body["children"][0][2] += 1
                    body["count"] += 1
                forged = self._compact_replace_relation(descriptor, "occurrences", self._compact_reseal_page(body))
                with self.assertRaisesRegex(IndexIntegrityError, message):
                    tuple(self._view(forged).postings(self._prefix() + ("a",)))
                self.connection.rollback()

    def test_paging_uses_numeric_revisions_and_the_frozen_descriptor(self):
        descriptor = self._publish(tuple(("retained_history", self._source("evidence.same", "needle", revision=revision))
                                         for revision in (10, 1, 2)))
        first, more = index_module.OwnerContentIndex.page(self._view(descriptor).search_field(
            directory_prefix=self._prefix(), query="needle"), 2)
        self.assertEqual(tuple(item.key[2] for item in first), (1, 2))
        self.assertTrue(more)
        successor = self._publish((("retained_history",
            self._source("evidence.same", "needle", revision=3, origin=11)),), descriptor)
        remaining, more = index_module.OwnerContentIndex.page(self._view(descriptor).search_field(
            directory_prefix=self._prefix(), query="needle", after=first[-1].key), 2)
        self.assertEqual(tuple(item.key[2] for item in remaining), (10,))
        self.assertFalse(more)
        current = tuple(self._view(successor).search_field(
            directory_prefix=self._prefix(), query="needle", after=first[-1].key))
        self.assertEqual(tuple(item.key[2] for item in current), (3, 10))
        old_cut = tuple(self._view(successor).search_field(
            directory_prefix=self._prefix(), query="needle", origin_commit_at_most=2))
        self.assertEqual(tuple(item.key[2] for item in old_cut), (1, 2))

    def test_descriptor_inventory_retains_owners_with_no_lexical_membership(self):
        inventory = tuple(("retained_history", self._source("evidence.empty", "", revision=revision))
                          for revision in (10, 1, 2))
        descriptor = self._publish(inventory)
        directory = index_module.inventory_directory_key(
            PROJECT, MISSION, "retained_history", "root", "evidence")
        page, more = index_module.OwnerContentIndex.page(self._view(descriptor).postings(directory), 2)
        self.assertEqual(tuple(posting.key[2] for posting in page), (1, 2))
        self.assertTrue(more)
        self.assertEqual(self._keys(descriptor, "empty"), ())
        self._audit(descriptor, inventory)
        with self.assertRaises(ValueError):
            tuple(self._view(descriptor).search_field(directory_prefix=self._prefix(), query=""))
        with self.assertRaisesRegex(ValueError, "^nonlexical postings cannot contain lexical positions$"):
            index_module._validate_posting_value(page[0].key,
                replace(page[0], positions=(0,)).to_mapping(), directory)

    def test_removals_require_exact_preimages_and_replacements_require_removal(self):
        from research_core.owner_content_access import OwnerContentDelta
        source = self._source("evidence.first", "a")
        descriptor = self._publish((("retained_history", source),))
        before = self._rows()
        changed = replace(source, payload_digest="f" * 64)
        missing = self._source("evidence.missing", "a")
        wrong_fields = replace(source, fields={"root": {**source.fields["root"], "content": "b"}})
        for delta in (
            OwnerContentDelta(removed_retained=(missing,)),
            OwnerContentDelta(removed_retained=(changed,)),
            OwnerContentDelta(removed_retained=(wrong_fields,)),
            OwnerContentDelta(added_retained=(changed,)),
        ):
            with self.subTest(delta=delta):
                self.connection.execute("BEGIN")
                with self.assertRaises(IndexIntegrityError):
                    self._view(descriptor).apply_delta(delta)
                self.assertEqual(self._rows(), before)
                self.connection.rollback()
        self.connection.execute("BEGIN")
        unchanged = self._view(descriptor)
        self.assertEqual(unchanged.apply_delta(OwnerContentDelta(added_retained=(source,))), descriptor)
        self.assertEqual(unchanged.nodes_written, 0)
        replacement = self._view(descriptor)
        replaced = replacement.apply_delta(OwnerContentDelta(
            removed_retained=(source,), added_retained=(changed,)))
        self.connection.commit()
        directory, key = self._prefix() + ("a",), ("evidence", source.identity, source.revision)
        self.assertEqual(self._view(replaced).posting(directory, key).payload_digest, "f" * 64)
        self.assertEqual(self._view(descriptor).posting(directory, key).payload_digest, source.payload_digest)
        self._audit(replaced, (("retained_history", changed),))

        # Replay also includes ordinary rows beyond the owner inventory:
        # current field-presence predicates and Capture's typed selector.
        candidate = self._source("candidate.replay", "", kind="candidate", document={"gaps": ["remaining"]})
        capture = self._source("capture.replay", "capture fact", kind="capture", revision=0,
                               document={"executive_epoch_id": None, "capture_kind": "output"})
        capture = replace(capture, inventory_metadata={"capture_descriptor": {
            "id": capture.handle, "origin_epoch_id": None, "capture_kind": "output",
        }})
        inventory = (("retained_history", changed), *((scope, item)
            for scope in ("current_at_cut", "retained_history") for item in (candidate, capture)))
        with mock.patch.object(pages_module.PageTree, "posting",
                               side_effect=AssertionError("new sources need no ordinary replay lookup")):
            replay_root = self._publish(inventory[1:], replaced)
        replay_rows = self._rows()
        for delta in (
            OwnerContentDelta(added_current=(candidate, capture)),
            OwnerContentDelta(added_retained=(candidate, capture)),
            OwnerContentDelta(added_current=(candidate, capture), added_retained=(candidate, capture)),
        ):
            with self.subTest(replay=delta):
                self.connection.execute("BEGIN")
                replay = self._view(replay_root)
                self.assertEqual(replay.apply_delta(delta), replay_root)
                self.assertEqual(replay.nodes_written, 0)
                self.assertEqual(self._rows(), replay_rows)
                self.connection.rollback()
        for delta in (
            OwnerContentDelta(added_current=(replace(capture, payload_digest="e" * 64),)),
            OwnerContentDelta(added_retained=(replace(candidate, payload_digest="e" * 64),)),
        ):
            with self.subTest(collision=delta):
                self.connection.execute("BEGIN")
                with self.assertRaises(IndexIntegrityError):
                    self._view(replay_root).apply_delta(delta)
                self.assertEqual(self._rows(), replay_rows)
                self.connection.rollback()
        self._audit(replay_root, inventory)

    def test_five_presence_predicates_preserve_bool_meaning_without_harvesting_prose(self):
        from research_core.owner_content_access import OwnerContentDelta, source_entries
        def source(kind, document, revision=1):
            return self._source(kind + ".first", "", revision=revision, kind=kind, document=document)
        def presence(value):
            return tuple((directory, posting) for directory, posting in source_entries(value, "current_at_cut")
                         if directory[0] == "current_field_presence")
        candidate = source("candidate", {
            "obligations": ["one"], "gaps": "legacy truthy field",
            "objections": {"legacy": False}, "circularity_risks": 1,
            "question": "not a fifth Candidate predicate",
        })
        context = source("context", {
            "known_omissions": ["one"],
            "treatments": {"unfinished": {"question": "not a checkpoint pointer"}},
            "gaps": ["not a Context predicate"],
        })
        self.assertEqual({(directory[3], directory[4]) for directory, _ in presence(candidate) + presence(context)}, {
            ("candidate", "obligations"), ("candidate", "gaps"), ("candidate", "objections"),
            ("candidate", "circularity_risks"), ("context", "known_omissions"),
        })
        for value in (None, False, 0, "", [], {}):
            with self.subTest(value=value):
                self.assertEqual(presence(source("candidate", {"gaps": value})), ())
                self.assertEqual(presence(source("context", {"known_omissions": value})), ())
        self.assertEqual(presence(source("context", {"treatments": {"unfinished": "open"}})), ())
        self.assertEqual(presence(source("branch", {"obligations": ["not applicable"]})), ())
        descriptor = self._publish((("current_at_cut", candidate), ("current_at_cut", context)))
        replacement = source("candidate", {"gaps": ["new"]}, 2)
        self.connection.execute("BEGIN")
        successor = self._view(descriptor).apply_delta(OwnerContentDelta(
            removed_current=(candidate,), added_current=(replacement,)))
        self.connection.commit()
        observed = tuple((directory, posting) for directory, posting in self._view(successor).iter_entries()
                         if directory[0] == "current_field_presence")
        self.assertEqual({directory[4] for directory, _ in observed}, {"gaps", "known_omissions"})
        self.assertEqual(sum(directory[0] == "current_field_presence"
                             for directory, _ in self._view(descriptor).iter_entries()), 5)
        self.assertEqual(self._keys(descriptor, "one", scope="current_at_cut", kind="candidate"), ())
        self._audit(successor, (("current_at_cut", replacement), ("current_at_cut", context)))

    def test_input_and_returned_metadata_cannot_mutate_authenticated_cached_values(self):
        with mock.patch.object(index_module, "_posting_from_value", side_effect=AssertionError("field validation constructed an outward Posting")):
            directory, basic = self._entries("evidence.first", "a")[0]
        metadata = {"qualifications": ({"scope": ["fixed order", "π", -0.0, 1e-7]},)}
        supplied = replace(basic, metadata=metadata)

        class Scalar(Enum):
            REFERENCE = basic.reference
            DIGEST = basic.payload_digest
            ONE = 1
            ZERO = 0

        normalized = replace(
            basic, reference=Scalar.REFERENCE, payload_digest=Scalar.DIGEST,
            origin_commit=Scalar.ONE, source_origin_commit=Scalar.ONE,
            positions=(Scalar.ZERO,), field_length=Scalar.ONE,
            metadata=MappingProxyType({
                "nested": (MappingProxyType({"text": "π😀\x00\\\"", "values": (
                    -0.0, 1.0, 1e-7, 2 ** 80, 10 ** 100, True, None,
                )}),),
            }),
        )
        expected_normalized = {
            "reference": "evidence:evidence.first@1",
            "payload_digest": hashlib.sha256(b"evidence:evidence.first@1").hexdigest(),
            "origin_commit": 1, "source_origin_commit": 1,
            "positions": [0], "field_length": 1,
            "metadata": {"nested": [{"text": "π😀\x00\\\"", "values": [
                -0.0, 1.0, 1e-7, 2 ** 80, 10 ** 100, True, None,
            ]}]},
        }
        self.assertEqual(normalized.to_mapping(), expected_normalized)
        self.assertEqual(normalized._canonical_bytes(), canonical_json_bytes(expected_normalized))
        # Scratch may reuse the first canonical encoding only if it is byte
        # identical to the former normalized-value re-encoding. Non-metadata
        # Enums must still normalize before posting validation.
        for candidate in (basic, supplied, normalized):
            with self.subTest(posting=candidate):
                expected_bytes = canonical_json_bytes(candidate.to_mapping())
                self.assertEqual(candidate._canonical_bytes(), expected_bytes)
                with index_module._SortedEntries(((directory, candidate),)) as ordered:
                    stored = ordered._database().execute("SELECT value_json FROM entries").fetchone()[0]
                    self.assertEqual(stored.encode("utf-8"), expected_bytes)
                    self.assertEqual(next(ordered.iter_entries())[1].to_mapping(), candidate.to_mapping())
        detached = normalized.to_mapping()
        detached["metadata"]["nested"][0]["values"].append("caller mutation")
        self.assertNotEqual(detached, normalized.to_mapping())
        for candidate, error in (
            (replace(basic, metadata={"value": Scalar.ONE}), ValueError),
            (replace(basic, metadata={"value": float("nan")}), ValueError),
            (replace(basic, metadata={"value": float("inf")}), ValueError),
            (replace(basic, metadata={"value": b"unsupported"}), ValueError),
            (replace(basic, metadata={"value": "\ud800"}), UnicodeEncodeError),
            (replace(basic, reference=b"unsupported"), TypeError),
            (replace(basic, reference="\ud800"), UnicodeEncodeError),
        ):
            with self.subTest(invalid=candidate):
                with self.assertRaises(error):
                    candidate.to_mapping()
                with self.assertRaises(error):
                    with index_module._SortedEntries(((directory, candidate),)):
                        self.fail("invalid posting was accepted")
        from research_core.owner_content_access import OwnerContentDelta
        capture = self._source("capture.first", "a", kind="capture", revision=0,
                               document={"executive_epoch_id": None, "capture_kind": "output"})
        capture = replace(capture, inventory_metadata={"capture_descriptor": {
            "id": capture.handle, "origin_epoch_id": None, "capture_kind": "output",
            "qualifications": metadata["qualifications"],
        }})
        descriptor = self._publish((("current_at_cut", capture),))
        metadata["qualifications"][0]["scope"].append("caller-side mutation")
        for digest, (_kind, encoded) in self._rows().items():
            body = json.loads(encoded)
            self.assertEqual(encoded.encode("utf-8"), canonical_json_bytes(body))
            self.assertEqual(digest, index_module._hash(body))
        directory = index_module.inventory_directory_key(PROJECT, MISSION, "current_at_cut", "root", "capture")
        key = ("capture", "capture.first", 0)
        view = self._view(descriptor)
        returned = view.posting(directory, key)
        expected = [{"scope": ["fixed order", "π", -0.0, 1e-7]}]
        self.assertEqual(returned.metadata["capture_descriptor"]["qualifications"], expected)
        returned.metadata["capture_descriptor"]["qualifications"][0]["scope"].append("result-side mutation")
        self.assertEqual(view.posting(directory, key).metadata["capture_descriptor"]["qualifications"], expected)
        self.assertEqual(self._view(descriptor).posting(directory, key).metadata["capture_descriptor"]["qualifications"], expected)
        lexical_directory = self._prefix(scope="current_at_cut", kind="capture") + ("a",)
        lexical = view.posting(lexical_directory, key)
        lexical.metadata["reference"]["identity"] = "caller-mutated"
        self.assertEqual(view.posting(lexical_directory, key).metadata["reference"]["identity"], "capture.first")
        self._audit(descriptor, (("current_at_cut", capture),))
        self.connection.execute("BEGIN")
        empty = view.apply_delta(OwnerContentDelta(removed_current=(capture,)))
        self.assertEqual(empty, index_module.empty_descriptor(PROFILE))
        self.connection.commit()
        self._audit(empty, ())

    def test_rare_and_absent_query_node_reads_do_not_scan_growing_posting_populations(self):
        descriptor = None
        observations = {"needle": [], "zzq": []}
        routed_observations = {query: [] for query in observations}
        measurements = []
        manifest_scope = pages_module.OccurrenceIndex._ROOT
        event_scope = manifest_scope + ("events",)
        occurrence_scope = manifest_scope + ("occurrences",)
        rare_map_scope = ("occurrence-field-map", PROJECT, MISSION, "root", "evidence", "evidence.rare")
        original_node = pages_module.PageTree._node

        def read_query(query, *, scan_events=False):
            statements, routed = [], {}

            def counted_node(tree, root, scope, *, children=True):
                # A routed visit authenticates its immediate children too.
                # Those children=False calls are bounded page fanout, not
                # additional descents into their unrelated subtrees.
                if children:
                    routed[scope] = routed.get(scope, 0) + 1
                return original_node(tree, root, scope, children=children)

            self.connection.set_trace_callback(statements.append)
            try:
                with mock.patch.object(pages_module.PageTree, "_node", counted_node):
                    view = self._view(descriptor)
                    if scan_events:
                        # Discriminator: real authenticated population reads,
                        # not fabricated counters or changed query results.
                        with view.operation():
                            engine = view._engine
                            roots = engine._roots(descriptor["root"])
                            for _entry in engine._items(roots["events"], event_scope):
                                pass
                    page, more = view.page(view.search_field(directory_prefix=self._prefix(), query=query), 2)
            finally:
                self.connection.set_trace_callback(None)
            self.assertEqual(tuple(posting.key[1] for posting in page),
                             ("evidence.rare",) if query == "needle" else ())
            self.assertFalse(more)
            reads = [statement for statement in statements if statement.startswith("SELECT")]
            self.assertTrue(reads)
            self.assertTrue(all("WHEREnode_digest=" in statement.replace(" ", "") for statement in reads))
            self.assertEqual(len(reads), view.node_reads)
            return view.node_reads, routed

        def assert_routed_work(query, routed, geometry):
            # This fixture has one retained revision per owner and no ordinary
            # owner contains any needle trigram. Each initial rare gram takes
            # three event routes and two occurrence routes. Resolving its one
            # result adds one occurrence route; exhaustion adds two of each.
            # An absent gram takes only floor/lower event routes. A route visits
            # at most height+1 nodes, independently of posting population.
            grams = len({query[offset:offset + 3] for offset in range(len(query) - 2)})
            event_routes = 3 * grams + 2 if query == "needle" else 2
            occurrence_routes = 2 * grams + 3 if query == "needle" else 0
            self.assertLessEqual(routed.get(event_scope, 0),
                                 event_routes * (geometry["event_height"] + 1), "events routed work")
            self.assertLessEqual(routed.get(occurrence_scope, 0),
                                 occurrence_routes * (geometry["occurrence_height"] + 1), "occurrences routed work")
            allowed = {manifest_scope, event_scope, occurrence_scope}
            if query == "needle":
                # The six-character rare source has one leaf-sized field map:
                # one binding authentication and one exact lookup per gram.
                self.assertLessEqual(routed.get(rare_map_scope, 0), grams + 1, "rare map routed work")
                allowed.add(rare_map_scope)
            self.assertFalse(set(routed) - allowed, "query visited an unrelated relation or owner map")

        previous_size = 0
        for size in (96, 384):
            additions = [("retained_history", self._source(f"evidence.ordinary.{ordinal:04}", "ordinary"))
                         for ordinal in range(previous_size, size)]
            if descriptor is None:
                additions.append(("retained_history", self._source("evidence.rare", "needle")))
            descriptor = self._publish(additions, descriptor)
            for query in observations:
                count, routed = read_query(query)
                observations[query].append(count)
                routed_observations[query].append(routed)
            # Inspect only the two relation roots outside the measured query
            # intervals. Their height/fanout distinguishes compact-page shape
            # changes without traversing the unrelated posting population.
            events = self._compact_relation_body(descriptor, "events")
            occurrences = self._compact_relation_body(descriptor, "occurrences")
            measurements.append({
                "population": size,
                "needle_node_reads": observations["needle"][-1],
                "zzq_node_reads": observations["zzq"][-1],
                "event_height": events["height"],
                "event_root_children": len(events.get("children", ())),
                "occurrence_height": occurrences["height"],
                "occurrence_root_children": len(occurrences.get("children", ())),
            })
            previous_size = size
        # These fixed numeric observations survive a subsequent scaling
        # assertion failure; they neither relax the caps nor pass the test.
        print("MR_COMPACT_QUERY_READS=" + json.dumps({
            "measurements": measurements,
            "limits": ["Two synthetic populations; keyed query reads, not retained-corpus performance."],
        }, sort_keys=True))
        for query, counts in observations.items():
            with self.subTest(query=query, node_reads=counts):
                self.assertLess(max(counts), 96)
                for routed, geometry in zip(routed_observations[query], measurements):
                    assert_routed_work(query, routed, geometry)
        # Correct empty results and digest-keyed SQL alone would admit a scan.
        # The structural oracle must reject one on the larger real fixture,
        # independently of the unchanged absolute node-read cap above.
        _scan_reads, scanned = read_query("zzq", scan_events=True)
        with self.assertRaisesRegex(AssertionError, "events routed work"):
            assert_routed_work("zzq", scanned, measurements[-1])

    def test_persistence_uses_and_rolls_back_with_the_callers_transaction(self):
        from research_core.owner_content_access import OwnerContentDelta
        descriptor = self._publish((("retained_history", self._source("evidence.first", "a")),))
        committed_rows = self._rows()
        source = self._source("evidence.second", "new fact", revision=2)
        mutation = self._view(descriptor)
        with self.assertRaisesRegex(IndexIntegrityError, "writer transaction"):
            mutation.apply_delta(OwnerContentDelta(added_retained=(source,)))
        self.assertEqual(self._rows(), committed_rows)
        self.connection.execute("BEGIN")
        uncommitted = mutation.apply_delta(OwnerContentDelta(added_retained=(source,)))
        self.assertTrue(self.connection.in_transaction)
        self.assertGreater(mutation.nodes_written, 0)
        self.assertEqual(self._keys(uncommitted, "new"), (("evidence", "evidence.second", 2),))
        self.connection.rollback()
        self.assertEqual(self._rows(), committed_rows)
        self.assertEqual(self._keys(descriptor, "a"), (("evidence", "evidence.first", 1),))
        with self.assertRaisesRegex(IndexIntegrityError, "absent"):
            self._view(uncommitted)

    def test_bulk_bootstrap_matches_incremental_contents_queries_pages_and_old_roots(self):
        from research_core.owner_content_access import OwnerContentDelta, source_entries
        old_source = self._source("evidence.old", "old retained root")
        old = self._publish((("retained_history", old_source),))
        bodies = {"evidence.a": "abcde", "evidence.b": "abc--bcd--cde",
                  "evidence.ß": "Straße", "evidence.empty": ""}
        inventory = [("retained_history", old_source)]
        for identity, body in bodies.items():
            for revision in (2, 10):
                inventory.append(("retained_history", self._source(
                    identity, body, revision=revision, variant=("root", "delegated"))))
        inventory.append(("current_at_cut", self._source("candidate.first", "", kind="candidate",
                         document={"gaps": ["unresolved qualification"]})))
        incremental = self._publish(inventory[1:], old)
        committed_rows = self._rows()
        with closing(sqlite3.connect(":memory:")) as destination:
            destination.execute("CREATE TABLE owner_content_index_node ("
                                "node_digest TEXT PRIMARY KEY, node_kind TEXT, node_json TEXT)")
            bulk = index_module.OwnerContentIndex(destination, PROFILE, integrity_error=IndexIntegrityError)
            self.assertIsInstance(bulk._engine, pages_module.OccurrenceIndex)
            destination.execute("BEGIN")
            with mock.patch.object(bulk._engine, "build", wraps=bulk._engine.build) as build:
                descriptor = bulk.bootstrap(iter(reversed(inventory)))
            build.assert_called_once()
            self.assertTrue(destination.in_transaction)
            destination.commit()
            reference = self._view(incremental)
            def logical(view):
                return [(directory, posting.key, posting.to_mapping()) for directory, posting in view.iter_entries()]
            self.assertEqual(logical(bulk), logical(reference))
            expected_count = len({(directory, posting.key) for scope, source in inventory
                                  for directory, posting in source_entries(source, scope)})
            with mock.patch.object(bulk._engine, "audit_sources", wraps=bulk._engine.audit_sources) as audit:
                self.assertEqual(bulk.audit_sources(iter(inventory)), expected_count)
            audit.assert_called_once()
            self.assertEqual(reference.audit_sources(iter(inventory)), expected_count)
            def pages(view, query, variant):
                result, after = [], None
                while True:
                    page, more = view.page(view.search_field(
                        directory_prefix=self._prefix(variant=variant), query=query, after=after), 2)
                    result.extend(posting.to_mapping() for posting in page)
                    if not more:
                        return result
                    self.assertEqual(len(page), 2)
                    self.assertNotEqual(after, page[-1].key)
                    after = page[-1].key
            for query in ("abcde", "abc", "SS", "Straße", "absent"):
                for variant in ("root", "delegated"):
                    with self.subTest(query=query, variant=variant):
                        self.assertEqual(pages(bulk, query, variant), pages(reference, query, variant))
                        self.assertEqual(tuple(item.key for item in bulk.search_field(
                            directory_prefix=self._prefix(variant=variant), query=query, origin_commit_at_most=2)),
                            tuple(item.key for item in reference.search_field(
                                directory_prefix=self._prefix(variant=variant), query=query, origin_commit_at_most=2)))
            addition = self._source("evidence.after-bulk", "new qualification", revision=11)
            delta = OwnerContentDelta(added_retained=(addition,))
            destination.execute("BEGIN")
            self.connection.execute("BEGIN")
            with mock.patch.object(bulk._engine, "replace_source", wraps=bulk._engine.replace_source) as replace_source:
                updated = bulk.apply_delta(delta)
            replace_source.assert_called_once()
            updated_reference = reference.apply_delta(delta)
            destination.commit()
            self.connection.commit()
            self.assertEqual(logical(bulk), logical(self._view(updated_reference)))
            frozen = index_module.OwnerContentIndex(destination, PROFILE, descriptor, integrity_error=IndexIntegrityError)
            self.assertEqual(frozen.audit_sources(iter(inventory)), expected_count)
            self.assertNotEqual(updated, descriptor)
            bulk.audit_sources(iter((*inventory, ("retained_history", addition))))
        self.assertTrue(all(self._rows()[digest] == value for digest, value in committed_rows.items()))
        self.assertEqual(self._keys(old, "retained"), (("evidence", "evidence.old", 1),))
        self._audit(old, (("retained_history", old_source),))

    def test_sorted_entries_keep_python_tuple_order_first_equal_values_and_close_scratch(self):
        entries = []
        for mission in ('mission."quoted"', "mission.\\path", "mission.\x00", "mission.é", "mission.😀"):
            for identity in ('evidence."quoted"', "evidence.\\path", "evidence.\x00", "evidence.z", "evidence.é", "evidence.😀"):
                for revision in (10, 2 ** 63 + 17, 2):
                    entries.extend(index_module.entries_for_inventory(
                        project_id=PROJECT, mission_id=mission, revision_scope="retained_history",
                        variant="root", kind="evidence", owner_id=identity, revision=revision,
                        reference=f"evidence:{identity}@{revision}", payload_digest="a" * 64,
                        origin_commit=1,
                    ))
        directory, posting = entries[0]
        first = (directory, replace(posting, metadata={"integer": 1, "zero": -0.0}))
        equal = (directory, replace(posting, metadata={"integer": True, "zero": 0}))
        entries[0] = first
        expected = sorted(entries, key=lambda entry: (entry[0], entry[1].key))
        expected_groups = {}
        for key, _posting in expected:
            expected_groups[key] = expected_groups.get(key, 0) + 1
        opened = []
        paths = []
        connect = sqlite3.connect

        def scratch(*args, **kwargs):
            self.assertEqual(len(args), 1)
            path = Path(args[0])
            self.assertEqual(path.name, "main.sqlite3")
            self.assertTrue(path.parent.name.startswith("rh-owner-content-"))
            self.assertTrue(path.parent.is_dir())
            paths.append(path)
            connection = connect(*args, **kwargs)
            opened.append(connection)
            self.assertTrue(path.is_file())
            return connection

        before_databases = tuple(self.connection.execute("PRAGMA database_list"))
        encoded_values = 0

        def encode(value):
            nonlocal encoded_values
            if isinstance(value, dict) and set(value) == {
                "reference", "payload_digest", "origin_commit", "source_origin_commit",
                "positions", "field_length", "metadata",
            }:
                encoded_values += 1
            return canonical_json_bytes(value)

        with mock.patch.object(index_module.sqlite3, "connect", side_effect=scratch), mock.patch.object(
            index_module, "canonical_json_bytes", side_effect=encode,
        ):
            # Keep first ahead of its numerically equal duplicate, while the
            # remaining input deliberately differs from Python key order.
            with index_module._SortedEntries(
                (first, *reversed(entries[1:]), equal, first), integrity_error=IndexIntegrityError,
            ) as ordered:
                # Includes equal duplicate attempts: scratch encodes each
                # supplied value once, not again after strict normalization.
                self.assertEqual(encoded_values, len(entries) + 2)
                self.assertEqual(opened[-1].execute("PRAGMA cache_size").fetchone()[0], -2048)
                self.assertTrue(paths[-1].is_file())
                self.assertGreater(paths[-1].stat().st_size, 0)
                actual = list(ordered.iter_entries())
                self.assertEqual(
                    [(key, item.key) for key, item in actual],
                    [(key, item.key) for key, item in expected],
                )
                self.assertEqual(ordered.directory_count, len(expected_groups))
                self.assertEqual(list(ordered.groups()), list(expected_groups.items()))
                selected = next(item for key, item in actual if (key, item.key) == (directory, posting.key))
                self.assertEqual(canonical_json_bytes(selected.to_mapping()), canonical_json_bytes(first[1].to_mapping()))
            with self.assertRaises(IndexIntegrityError):
                with index_module._SortedEntries(
                    (first, (directory, replace(posting, payload_digest="b" * 64))),
                    integrity_error=IndexIntegrityError,
                ):
                    self.fail("unequal duplicate posting was accepted")
        failed_paths = []

        def fail_to_connect(path):
            failed_paths.append(Path(path))
            self.assertTrue(Path(path).parent.is_dir())
            raise sqlite3.OperationalError("fixture connection failure")

        with mock.patch.object(index_module.sqlite3, "connect", side_effect=fail_to_connect):
            with self.assertRaisesRegex(sqlite3.OperationalError, "fixture connection failure"):
                with index_module._SortedEntries((first,), integrity_error=IndexIntegrityError):
                    self.fail("failed scratch connection was accepted")
        self.assertEqual(len(opened), 2)
        for connection in opened:
            with self.assertRaises(sqlite3.ProgrammingError):
                connection.execute("SELECT 1")
        self.assertEqual(len(failed_paths), 1)
        for path in paths + failed_paths:
            self.assertFalse(path.exists())
            self.assertFalse(path.parent.exists())
        self.assertEqual(tuple(self.connection.execute("PRAGMA database_list")), before_databases)
        self.assertEqual(self._rows(), {})

        encoded_values = 0
        generated = index_module.entries_for_fields(
            project_id=PROJECT, mission_id=MISSION, revision_scope="retained_history",
            variant="root", kind="evidence", owner_id="evidence.generated",
            revision=1, reference="evidence:evidence.generated@1", payload_digest="b" * 64,
            origin_commit=1, fields={"content": "aba"},
        )
        with mock.patch.object(index_module, "canonical_json_bytes", side_effect=encode):
            with index_module._SortedEntries(generated) as ordered:
                count = ordered._database().execute("SELECT COUNT(*) FROM entries").fetchone()[0]
                self.assertEqual(count, 5)
                # Existing generator validation remains: one encoding there
                # and one in scratch, rather than the former three per value.
                self.assertEqual(encoded_values, 2 * count)

    def test_bulk_bootstrap_requires_empty_transaction_and_preserves_rollback_and_collisions(self):
        self.connection.execute("BEGIN")
        empty = self._view().bootstrap(iter(()))
        self.assertEqual(empty, index_module.empty_descriptor(PROFILE))
        self.assertTrue(self.connection.in_transaction)
        self._audit(empty, ())
        self.connection.rollback()
        self.assertEqual(self._rows(), {})
        inventory = (("retained_history", self._source("evidence.new", "different bootstrap graph", revision=2)),)
        with self.assertRaisesRegex(IndexIntegrityError, "writer transaction"):
            self._view().bootstrap(iter(inventory))
        self.assertFalse(self.connection.in_transaction)
        self.assertEqual(self._rows(), {})
        self.connection.execute("BEGIN")
        uncommitted = self._view().bootstrap(iter(inventory))
        self.assertTrue(self.connection.in_transaction)
        self.assertEqual(self._keys(uncommitted, "bootstrap"), (("evidence", "evidence.new", 2),))
        self.connection.rollback()
        self.assertEqual(self._rows(), {})
        with self.assertRaisesRegex(IndexIntegrityError, "absent"):
            self._view(uncommitted)
        # Inject actual different bytes at one content address only after the
        # empty-storage admission. Ordinary collision detection and caller
        # rollback must discard both this row and any preceding child writes.
        self.connection.execute("BEGIN")
        collision = self._view()
        persist = collision._engine.packed._persist
        injected = []
        def collide(digest, kind, raw):
            if not injected:
                self.connection.execute("INSERT INTO owner_content_index_node VALUES (?, ?, ?)",
                                        (digest, kind, "{}"))
                injected.append(digest)
            return persist(digest, kind, raw)
        with mock.patch.object(collision._engine.packed, "_persist", side_effect=collide):
            with self.assertRaisesRegex(IndexIntegrityError, "persisted object collision"):
                collision.bootstrap(iter(inventory))
        self.assertTrue(injected)
        self.assertTrue(self.connection.in_transaction)
        self.connection.rollback()
        self.assertEqual(self._rows(), {})
        old = self._publish((("retained_history", self._source("evidence.old", "retained")),))
        committed = self._rows()
        self.connection.execute("BEGIN")
        for view in (self._view(), self._view(old)):
            with self.assertRaises(IndexIntegrityError):
                view.bootstrap(iter(inventory))
            self.assertEqual(self._rows(), committed)
        self.connection.rollback()
        self.assertEqual(self._keys(old, "retained"), (("evidence", "evidence.old", 1),))


if __name__ == "__main__":
    unittest.main()
