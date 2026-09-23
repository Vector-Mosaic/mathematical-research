"""Store-internal authenticated compact owner-content pages.

No table creation, source authorization, profile selection or transaction
commit occurs here. The owning Store supplies those boundaries. The index
facade imports this module lazily because shared logical primitives remain in
owner_content_index.
"""
from __future__ import annotations

import base64
import hashlib
import json
import re
import sqlite3
import time
from collections import OrderedDict
from collections.abc import Mapping
from contextlib import closing, contextmanager
from bisect import bisect_left, bisect_right
from itertools import pairwise, zip_longest
from types import MappingProxyType, SimpleNamespace

from . import owner_content_index as index_module
from .json_support import canonical_json_bytes
from .observability import report_preparation_progress

_SOURCE_FIELDS = ("reference", "payload_digest", "origin_commit", "source_origin_commit", "metadata")


def _native_json_bytes(value):
    """Encode caller-proven builtin JSON, without domain normalization.

    Only strict-decoded trees (and closed, explicitly typed scalar records)
    qualify. This is not a validator or a replacement for canonical_json_bytes:
    domain mappings and non-string object keys still require its normalization.
    Keep these options byte-identical to that owner's final JSON encoding.
    """
    return json.dumps(value, ensure_ascii=False, allow_nan=False, sort_keys=True,
                      separators=(",", ":")).encode("utf-8")



def _same_json(left, right):
    # Preserve JSON scalar types/qualifications, not Python's True == 1.
    # Lists and tuples have the same production canonical JSON meaning.
    if isinstance(left, Mapping) and isinstance(right, Mapping):
        return (left.keys() == right.keys()
                and all(_same_json(left[key], right[key]) for key in left))
    if isinstance(left, (list, tuple)) and isinstance(right, (list, tuple)):
        return (len(left) == len(right)
                and all(_same_json(a, b) for a, b in zip(left, right)))
    return type(left) is type(right) and left == right and (type(left) is not float or repr(left) == repr(right))


class PageObjectStore:
    """Connection-bound immutable object persistence; schema remains caller-owned."""

    def __init__(self, connection, *, profile_sha256, integrity_error, table,
                 digest_column, kind_column, body_column, domain):
        names = (table, digest_column, kind_column, body_column)
        if any(type(name) is not str or re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name) is None for name in names):
            raise ValueError("invalid owner content object SQL identifier")
        if not index_module._digest(profile_sha256) or type(domain) is not str or not domain:
            raise ValueError("invalid owner content object profile/domain")
        if not callable(integrity_error):
            raise TypeError("integrity_error must be callable")
        self.connection = connection
        self.profile_sha256, self.integrity_error, self._DOMAIN = profile_sha256, integrity_error, domain
        self.node_reads = self.nodes_written = 0
        self._insert_sql = f"INSERT INTO {table} ({digest_column},{kind_column},{body_column}) VALUES (?,?,?) ON CONFLICT DO NOTHING"
        self._select_sql = f"SELECT {kind_column},{body_column} FROM {table} WHERE {digest_column}=?"

    def _persist(self, digest, kind, raw):
        if self.connection is None:
            raise self.integrity_error("owner content pages: persistence requires a connection")
        inserted = self.connection.execute(self._insert_sql, (digest, kind, raw.decode("utf-8"))).rowcount
        self.nodes_written += int(bool(inserted))
        if not inserted:
            previous = self.connection.execute(self._select_sql, (digest,)).fetchone()
            if previous is None or tuple(previous) != (kind, raw.decode("utf-8")):
                raise self.integrity_error("owner content pages: persisted object collision")

    def _store(self, kind, **payload):
        body = {"domain": self._DOMAIN, "profile": self.profile_sha256, "kind": kind, **payload}
        raw = canonical_json_bytes(body)
        digest = hashlib.sha256(raw).hexdigest()
        self._persist(digest, kind, raw)
        return digest

    def _load(self, digest, kind):
        return self._load_record(digest, kind)[0]

    def _load_record(self, digest, kind):
        if self.connection is None:
            raise self.integrity_error("owner content pages: reads require a connection")
        self.node_reads += 1
        row = self.connection.execute(self._select_sql, (digest,)).fetchone()
        if row is None:
            raise self.integrity_error("owner content pages: referenced object is absent")
        try:
            body = index_module.loads_strict_json_object(row[1])
            raw = _native_json_bytes(body)
        except (ValueError, TypeError, UnicodeError) as exc:
            raise self.integrity_error("owner content pages: invalid object JSON") from exc
        if (row[0] != kind or body.get("kind") != kind or body.get("domain") != self._DOMAIN
                or body.get("profile") != self.profile_sha256 or raw.decode("utf-8") != row[1]
                or hashlib.sha256(raw).hexdigest() != digest):
            raise self.integrity_error("owner content pages: object authentication failed")
        return body, len(raw)



class PageTree:
    """Authenticated COW pages shared by occurrence and ordinary descriptors.

    The caller owns schema, transaction, profile and source authorization.
    Bootstrap spills whole-history ordering; individual sources/maps/vectors
    and explicit deltas can still materialize. Cache charges are encoded
    physical plus expanded views, not a bound on transient Python RSS.
    """

    _OVERFLOW = ("overflow",)
    _SUMMARY = {"digest", "kind", "count", "first", "last", "height", "used_bytes"}
    _COMPACT_CODEC = "tuple-prefix-v1"
    _CHILD_FIELDS = ("digest", "kind", "count", "first", "last", "height", "used_bytes")
    _VALIDATION_FLAG_BYTES = len(canonical_json_bytes({"basic": False, "children": False, "native_json": False}))

    def __init__(self, packed, *, page_bytes=4096, compact_page_keys=True,
                 cache_items_limit=128, cache_bytes_limit=2 * 1024 * 1024):
        if (type(page_bytes) is not int or page_bytes < 64
                or type(compact_page_keys) is not bool
                or type(cache_items_limit) is not int or cache_items_limit < 1
                or type(cache_bytes_limit) is not int or cache_bytes_limit < 1):
            raise ValueError("invalid owner content page/cache configuration")
        self.packed, self.page_bytes = packed, page_bytes
        self.integrity_error = packed.integrity_error
        self.compact_page_keys = compact_page_keys
        self._depth = 0
        self._cache, self._pending = OrderedDict(), {}
        self._cache_bytes = 0
        self.cache_items_limit, self.cache_bytes_limit = cache_items_limit, cache_bytes_limit
        self.peak_cache_items = self.peak_cache_bytes = 0
        self._bootstrap_scratch = None
        self._bootstrap_cursors = set()
        self._bootstrap_job = 0
        self.bootstrap_buffers = {}

    def operation(self):
        """One caller-owned page/operation lifetime; never persistent trust."""
        return self._operation()

    @contextmanager
    def _operation(self):
        self._depth += 1
        try:
            yield
        finally:
            self._depth -= 1
            if not self._depth:
                self._cache.clear()
                self._pending.clear()
                self._cache_bytes = 0

    def _cached(self, key):
        found = self._cache.get(key) if self._depth else None
        if found is not None:
            self._cache.move_to_end(key)
            return found[0]
        return None

    def _remember(self, key, value, size):
        if self._depth:
            if key in self._cache:
                self._cache_bytes -= self._cache.pop(key)[1]
            if size > self.cache_bytes_limit:
                return value
            self._cache[key] = (value, size)
            self._cache_bytes += size
            while len(self._cache) > self.cache_items_limit or self._cache_bytes > self.cache_bytes_limit:
                self._cache_bytes -= self._cache.popitem(last=False)[1][1]
            self.peak_cache_items = max(self.peak_cache_items, len(self._cache))
            self.peak_cache_bytes = max(self.peak_cache_bytes, self._cache_bytes)
        return value

    def _write(self, kind, references=(), **payload):
        if self._bootstrap_scratch is not None:
            # Bulk pages are final when sealed. Reuse the existing collision
            # owner, inside the caller's transaction; never commit here.
            self.bootstrap_buffers["pending_objects_peak"] = max(
                self.bootstrap_buffers["pending_objects_peak"], len(self._pending))
            return self.packed._store(kind, **payload)
        body = {"domain": self.packed._DOMAIN, "profile": self.packed.profile_sha256, "kind": kind, **payload}
        raw = canonical_json_bytes(body)
        digest = hashlib.sha256(raw).hexdigest()
        previous = self._pending.get(digest)
        if previous is not None and previous[1] != raw:
            raise self.integrity_error("owner content pages: staged object collision")
        self._pending[digest] = (body, raw, tuple(references))
        return digest

    def _object(self, digest, kind):
        key = ("object", digest, kind)
        record = self._cached(key)
        if record is None:
            if digest in self._pending:
                body, raw, _references = self._pending[digest]
                if body["kind"] != kind:
                    raise self.integrity_error("owner content pages: staged kind differs")
                size = len(raw)
                native_json = False  # Pending values can retain domain mappings.
            else:
                body, size = self.packed._load_record(digest, kind)
                native_json = True  # The strict decoder owns this builtin tree.
            # One body/validation record, not separate object/node/False/True
            # entries. Charge validation/provenance flags at their largest encoded
            # size; this is bounded encoded accounting, not a Python RSS claim.
            record = SimpleNamespace(body=body, basic=False, children=False, native_json=native_json, logical_body=None,
                cache_size=size + self._VALIDATION_FLAG_BYTES)
            self._remember(key, record, record.cache_size)
        return record

    def _load(self, digest, kind):
        return self._object(digest, kind).body

    def _commit(self, root):
        visited = set()

        def visit(digest):
            if digest in visited or digest not in self._pending:
                return
            visited.add(digest)
            body, raw, references = self._pending[digest]
            for reference in references:
                visit(reference)
            self.packed._persist(digest, body["kind"], raw)

        visit(root["digest"])
        return root

    def _encode(self, value):
        raw = canonical_json_bytes(value)
        if len(raw) <= self.page_bytes // 3:
            return {"inline": value}
        chunks = [((ordinal,), {"chunk": base64.b64encode(raw[offset:offset + self.page_bytes // 8]).decode("ascii")})
                  for ordinal, offset in enumerate(range(0, len(raw), self.page_bytes // 8))]
        return {"overflow": self._tree(self._OVERFLOW, chunks), "size": len(raw),
                "sha256": hashlib.sha256(raw).hexdigest()}

    def _decode(self, value):
        if set(value) == {"inline"}:
            return value["inline"]
        if set(value) != {"overflow", "size", "sha256"}:
            raise self.integrity_error("owner content pages: value reference shape differs")
        if not index_module._integer(value["size"]) or not index_module._digest(value["sha256"]):
            raise self.integrity_error("owner content pages: overflow length/digest differs")
        # Cached decoded bytes never authenticate a newly supplied parent
        # summary, even when digest/size/checksum still identify the same value.
        self._node(value["overflow"], self._OVERFLOW)
        key = ("value", value["overflow"]["digest"], value["sha256"], value["size"])
        decoded = self._cached(key)
        if decoded is None:
            chunks = []
            for ordinal, (index, item) in enumerate(self._items(value["overflow"], self._OVERFLOW)):
                if index != (ordinal,) or set(item) != {"chunk"}:
                    raise self.integrity_error("owner content pages: overflow sequence differs")
                chunks.append(base64.b64decode(item["chunk"], validate=True))
            raw = b"".join(chunks)
            if len(raw) != value["size"] or hashlib.sha256(raw).hexdigest() != value["sha256"]:
                raise self.integrity_error("owner content pages: overflow bytes differ")
            decoded = index_module.loads_strict_json_object(raw)
            self._remember(key, decoded, len(raw))
        return decoded

    def _seal(self, scope, kind, items, *, prepared=None):
        leaf = kind == "range-cell"
        field = "entries" if leaf else "children"
        if self.compact_page_keys:
            payload, used = self._compact_payload(kind, items, prepared=prepared)
        else:
            payload, used = {field: items}, len(canonical_json_bytes(items))
        if used > self.page_bytes:
            raise ValueError("owner content pages: routing key exceeds page capacity")
        count = len(items) if leaf else sum(child["count"] for child in items)
        first = (items[0]["key"] if leaf else items[0]["first"]) if items else None
        last = (items[-1]["key"] if leaf else items[-1]["last"]) if items else None
        height = 0 if leaf else items[0]["height"] + 1
        references = []
        if leaf:
            for item in items:
                encoded = item["value"]
                if "overflow" in encoded:
                    references.append(encoded["overflow"]["digest"])
                value = self._decode(encoded)
                references.extend(self._value_references(scope, tuple(item["key"]), value))
        else:
            references.extend(child["digest"] for child in items)
        digest = self._write(kind, references, scope=list(scope), count=count, first=first, last=last,
                             height=height, used_bytes=used, **payload,
                             **({"page_codec": self._COMPACT_CODEC} if self.compact_page_keys else {}))
        return {"digest": digest, "kind": kind, "count": count, "first": first, "last": last,
                "height": height, "used_bytes": used}

    def _value_references(self, scope, key, value):
        if scope == ():
            return [value["digest"]]
        if scope != self._OVERFLOW:
            return [value["source"]] + ([value["position_vector"]] if "position_vector" in value else [])
        return []
    def _page_key(self, value):
        # Check types BEFORE equality/common-prefix work: True is not revision1.
        if not isinstance(value, (list, tuple)) or any(
                type(part) is not str and not (type(part) is int and part >= 0) for part in value):
            raise self.integrity_error("owner content pages: page key has invalid components")
        return tuple(value)

    @staticmethod
    def _common_key_prefix(left, right):
        width = 0
        for a, b in zip(left, right):
            if type(a) is not type(b) or a != b:
                break
            width += 1
        return left[:width]

    def _compact_profile(self, kind, item):
        leaf = kind == "range-cell"
        if not isinstance(item, dict) or set(item) != ({"key", "value"} if leaf else self._SUMMARY):
            raise self.integrity_error("owner content pages: compact input shape differs")
        keys = [self._page_key(item["key"])] if leaf else [self._page_key(item[name]) for name in ("first", "last")]
        suffix_sizes, prefix_sizes = [], None
        for key in keys:
            lengths = [len(canonical_json_bytes(part)) for part in key]
            suffix, total = [0] * (len(key) + 1), sum(lengths)
            for width in range(len(key) + 1):
                suffix[width] = 2 + total + max(0, len(key) - width - 1)
                if width < len(key):
                    total -= lengths[width]
            suffix_sizes.append(suffix)
            if prefix_sizes is None:
                prefix_sizes, total = [2], 0
                for width, length in enumerate(lengths, 1):
                    total += length
                    prefix_sizes.append(2 + total + width - 1)
        static = (3 + len(canonical_json_bytes(item["value"])) if leaf else
                  8 + sum(len(canonical_json_bytes(item[name])) for name in self._CHILD_FIELDS if name not in {"first", "last"}))
        costs = [static + sum(sizes[width] for sizes in suffix_sizes)
                 for width in range(min(map(len, keys)) + 1)]
        prefix = keys[0]
        for key in keys[1:]:
            prefix = self._common_key_prefix(prefix, key)
        return SimpleNamespace(prefix=prefix, prefix_sizes=prefix_sizes, costs=costs)

    def _compact_state(self, kind, state, profile):
        if state is None:
            count, prefix, sizes, costs = 1, profile.prefix, profile.prefix_sizes, profile.costs
        else:
            count = state.count + 1
            prefix = self._common_key_prefix(state.prefix, profile.prefix)
            sizes = state.prefix_sizes
            costs = [a + b for a, b in zip(state.costs, profile.costs)]
        field = "entries" if kind == "range-cell" else "children"
        # Fixed JSON punctuation plus additive row/suffix lengths. No complete
        # candidate page is re-serialized on append or on each possible split.
        base = len('{"key_prefix":[],"' + field + '":[]}')
        size = base + sizes[len(prefix)] - 2 + costs[len(prefix)] + count - 1
        return SimpleNamespace(count=count, prefix=prefix, prefix_sizes=sizes, costs=costs, size=size)

    def _compact_payload(self, kind, items, *, prepared=None):
        # A finalized packing group already owns these exact row profiles.
        # Direct seals prepare once; neither path bypasses actual-byte checking.
        state = prepared
        if state is None:
            for item in items:
                state = self._compact_state(kind, state, self._compact_profile(kind, item))
        prefix = () if state is None else state.prefix
        width, leaf = len(prefix), kind == "range-cell"
        rows = ([[item["key"][width:], item["value"]] for item in items] if leaf else
                [[item[name][width:] if name in {"first", "last"} else item[name]
                  for name in self._CHILD_FIELDS] for item in items])
        payload = {"key_prefix": list(prefix), "entries" if leaf else "children": rows}
        actual = len(canonical_json_bytes(payload))
        if state is not None and actual != state.size:
            raise self.integrity_error("owner content pages: compact size accounting differs")
        return payload, actual

    def _compact_groups(self, items, kind):
        previous, previous_state, current, state = None, None, [], None
        for item in items:
            profile = self._compact_profile(kind, item)
            alone = self._compact_state(kind, None, profile)
            if alone.size > self.page_bytes:
                raise ValueError("owner content pages: routing key exceeds page capacity")
            proposed = self._compact_state(kind, state, profile)
            if current and proposed.size > self.page_bytes:
                if previous is not None:
                    yield [value for value, _profile in previous], previous_state
                previous, previous_state, current, state = current, state, [], None
                proposed = alone
            current.append((item, profile))
            state = proposed
            if self._bootstrap_scratch is not None:
                peak = state.size + (0 if previous_state is None else previous_state.size)
                self.bootstrap_buffers["tail_payload_bytes_peak"] = max(self.bootstrap_buffers["tail_payload_bytes_peak"], peak)
                self.bootstrap_buffers["tail_entries_peak"] = max(self.bootstrap_buffers["tail_entries_peak"],
                    len(current) + (0 if previous is None else len(previous)))
        if previous is not None:
            tail = previous + current
            suffix_states, suffix = [None] * (len(tail) + 1), None
            for ordinal in range(len(tail) - 1, -1, -1):
                suffix = self._compact_state(kind, suffix, tail[ordinal][1])
                suffix_states[ordinal] = suffix
            prefix, best = None, None
            for ordinal in range(1, len(tail)):
                prefix = self._compact_state(kind, prefix, tail[ordinal - 1][1])
                right = suffix_states[ordinal]
                if max(prefix.size, right.size) <= self.page_bytes:
                    choice = (abs(prefix.size - right.size), ordinal)
                    if best is None or choice < best[0]:
                        best = choice, prefix, right
            if best is not None:
                split = best[0][1]
                previous, current = tail[:split], tail[split:]
                previous_state, state = best[1:]
            yield [value for value, _profile in previous], previous_state
        if current:
            yield [value for value, _profile in current], state

    def _page_groups(self, items, kind=None, *, prepared=False):
        if self.compact_page_keys:
            for group, state in self._compact_groups(items, kind):
                yield (group, state) if prepared else group
            return
        # The only uncertain pages are the last completed page and its tail.
        # Earlier pages cannot participate in the existing final-pair balance.
        previous, previous_size, current, size = None, 0, [], 2
        for item in items:
            cost = len(canonical_json_bytes(item))
            if cost + 2 > self.page_bytes:
                raise ValueError("owner content pages: routing key exceeds page capacity")
            if current and size + cost + 1 > self.page_bytes:
                if previous is not None:
                    yield (previous, None) if prepared else previous
                previous, previous_size = current, size
                current, size = [], 2
            size += cost + bool(current)
            current.append(item)
            if self._bootstrap_scratch is not None:
                sizes = size + previous_size
                self.bootstrap_buffers["tail_payload_bytes_peak"] = max(
                    self.bootstrap_buffers["tail_payload_bytes_peak"], sizes)
                self.bootstrap_buffers["tail_entries_peak"] = max(
                    self.bootstrap_buffers["tail_entries_peak"], len(current) + (0 if previous is None else len(previous)))
        # Balance the tail instead of permanently retaining a tiny final page.
        if previous is not None:
            tail = previous + current
            costs = [len(canonical_json_bytes(item)) for item in tail]
            total, prefix, possible = sum(costs), 0, []
            for i in range(1, len(tail)):
                prefix += costs[i - 1]
                left, right = prefix + i + 1, total - prefix + len(tail) - i + 1
                if max(left, right) <= self.page_bytes:
                    possible.append((abs(left - right), i))
            if possible:
                split = min(possible)[1]
                previous, current = tail[:split], tail[split:]
            yield (previous, None) if prepared else previous
        if current:
            yield (current, None) if prepared else current

    def _pack(self, scope, kind, items):
        return [self._seal(scope, kind, group, prepared=state)
                for group, state in self._page_groups(items, kind, prepared=True)]

    def _finish(self, scope, pages):
        if not pages:
            return self._seal(scope, "range-cell", [])
        while len(pages) > 1:
            parents = self._pack(scope, "range-branch", pages)
            if len(parents) >= len(pages):
                raise ValueError("owner content pages: routing key prevents branching")
            pages = parents
        root = pages[0]
        while root["kind"] == "range-branch":
            children = self._node(root, scope)["children"]
            if len(children) != 1:
                break
            root = children[0]
        return root

    def _tree(self, scope, items):
        if self._bootstrap_scratch is not None:
            return self._bootstrap_tree(scope, items)
        return self._finish(scope, self._pack(scope, "range-cell", [
            {"key": list(key), "value": self._encode(value)} for key, value in items
        ]))

    def _bootstrap_tree(self, scope, items):
        # Level summaries spill into the SAME private sorted-input lifetime.
        # There is no full-level Python frontier or staged-object arena.
        scratch = self._bootstrap_scratch
        self._bootstrap_job += 1
        job, level, previous_count = self._bootstrap_job, 0, None
        values = ({"key": list(key), "value": self._encode(value)} for key, value in items)
        try:
            while True:
                count = 0
                kind = "range-cell" if level == 0 else "range-branch"
                for group, state in self._page_groups(values, kind, prepared=True):
                    page = self._seal(scope, kind, group, prepared=state)
                    scratch.execute("INSERT INTO occurrence_bootstrap_levels VALUES (?,?,?,?)",
                                    (job, level, count, canonical_json_bytes(page).decode("utf-8")))
                    count += 1
                self.bootstrap_buffers["level_rows_peak"] = max(self.bootstrap_buffers["level_rows_peak"], count)
                if level:
                    scratch.execute("DELETE FROM occurrence_bootstrap_levels WHERE job=? AND level=?", (job, level - 1))
                    if count >= previous_count:
                        raise ValueError("owner content pages: routing key prevents branching")
                if not count:
                    return self._seal(scope, "range-cell", [])
                if count == 1:
                    raw = scratch.execute("SELECT body FROM occurrence_bootstrap_levels WHERE job=? AND level=?",
                                          (job, level)).fetchone()[0]
                    return index_module.loads_strict_json_object(raw)
                previous_count = count
                cursor = self._bootstrap_select("SELECT body FROM occurrence_bootstrap_levels WHERE job=? AND level=? ORDER BY ordinal",
                                                (job, level))
                values = (index_module.loads_strict_json_object(raw) for (raw,) in cursor)
                level += 1
        finally:
            scratch.execute("DELETE FROM occurrence_bootstrap_levels WHERE job=?", (job,))

    def _posting_get(self, tree, scope, key):
        return self._get(tree, scope, key)

    def _posting_items(self, tree, scope, after=None):
        yield from self._items(tree, scope, after)

    def _node(self, root, scope, *, children=True):
        if (set(root) != self._SUMMARY or root["kind"] not in {"range-cell", "range-branch"}
                or not index_module._digest(root["digest"])
                or any(not index_module._integer(root[name]) for name in ("count", "height", "used_bytes"))):
            raise self.integrity_error("owner content pages: multiway summary shape differs")
        record = self._object(root["digest"], root["kind"])
        body = record.body
        encode_size = _native_json_bytes if record.native_json else canonical_json_bytes
        leaf = root["kind"] == "range-cell"
        field = "entries" if leaf else "children"
        compact = "page_codec" in body or "key_prefix" in body
        if compact:
            if (not self.compact_page_keys or body.get("page_codec") != self._COMPACT_CODEC
                    or set(body) != (self._SUMMARY - {"digest"}) | {"domain", "profile", "scope", field, "page_codec", "key_prefix"}
                    or not isinstance(body["key_prefix"], list) or not isinstance(body[field], list)):
                raise self.integrity_error("owner content pages: compact page codec or shape differs")
            if (body["scope"] != list(scope)
                    or any(body[name] != root[name] or type(body[name]) is not type(root[name])
                           for name in self._SUMMARY - {"digest"})):
                raise self.integrity_error("owner content pages: multiway scope/summary differs")
            for name in ("first", "last"):
                if root[name] is not None:
                    self._page_key(root[name])
                if body[name] is not None:
                    self._page_key(body[name])
            if not record.basic:
                payload = {"key_prefix": body["key_prefix"], field: body[field]}
                if len(encode_size(payload)) != root["used_bytes"] or root["used_bytes"] > self.page_bytes:
                    raise self.integrity_error("owner content pages: multiway page bytes differ")
            # Authenticate stored bytes first (_object), then reconstruct only
            # strict typed keys. Source/position/value codecs are unchanged.
            if record.logical_body is None:
                prefix = self._page_key(body["key_prefix"])
                items, common = [], None
                for row in body[field]:
                    if not isinstance(row, list) or len(row) != (2 if leaf else len(self._CHILD_FIELDS)):
                        raise self.integrity_error("owner content pages: compact row arity differs")
                    if leaf:
                        if not isinstance(row[0], list) or not isinstance(row[1], dict):
                            raise self.integrity_error("owner content pages: compact leaf row differs")
                        key = prefix + self._page_key(row[0])
                        item, keys = {"key": list(key), "value": row[1]}, (key,)
                    else:
                        item = dict(zip(self._CHILD_FIELDS, row))
                        keys = []
                        for name in ("first", "last"):
                            if not isinstance(item[name], list):
                                raise self.integrity_error("owner content pages: compact child fence differs")
                            key = prefix + self._page_key(item[name])
                            item[name] = list(key)
                            keys.append(key)
                    for key in keys:
                        common = key if common is None else self._common_key_prefix(common, key)
                    items.append(item)
                if prefix != (() if common is None else common):
                    raise self.integrity_error("owner content pages: compact prefix is not canonical")
                logical = {name: value for name, value in body.items() if name not in {"key_prefix", "page_codec"}}
                logical[field] = items
                record.logical_body = logical
                # Retain one record but charge BOTH physical and reconstructed
                # canonical byte extents, conservatively counting shared values
                # twice. An over-budget decoded view evicts the raw record too.
                record.cache_size += len(encode_size(logical))
                self._remember(("object", root["digest"], root["kind"]), record, record.cache_size)
            body = record.logical_body
        # A cached body never vouches for a newly supplied parent summary or
        # scope. Check this binding on EVERY call, including fully warm nodes.
        if (set(body) != (self._SUMMARY - {"digest"}) | {"domain", "profile", "scope", field}
                or body["scope"] != list(scope)
                or any(body[name] != root[name] or type(body[name]) is not type(root[name]) for name in self._SUMMARY - {"digest"})):
            raise self.integrity_error("owner content pages: multiway scope/summary differs")
        items = body[field]
        if not record.basic:
            if not compact and (len(encode_size(items)) != root["used_bytes"] or root["used_bytes"] > self.page_bytes):
                raise self.integrity_error("owner content pages: multiway page bytes differ")
            if leaf:
                keys = [tuple(item["key"]) for item in items]
                if (any(set(item) != {"key", "value"} for item in items) or root["height"] != 0
                        or len(keys) != root["count"] or any(a >= b for a, b in zip(keys, keys[1:]))
                        or root["first"] != (list(keys[0]) if keys else None)
                        or root["last"] != (list(keys[-1]) if keys else None)):
                    raise self.integrity_error("owner content pages: multiway cell order/count differs")
                record.children = True  # A leaf has no additional child proof.
            else:
                if (not items or any(set(item) != self._SUMMARY or not item["count"] for item in items)
                        or any(item["height"] + 1 != root["height"] for item in items)
                        or any(tuple(a["last"]) >= tuple(b["first"]) for a, b in zip(items, items[1:]))
                        or sum(item["count"] for item in items) != root["count"]
                        or items[0]["first"] != root["first"] or items[-1]["last"] != root["last"]):
                    raise self.integrity_error("owner content pages: multiway branch range/count differs")
            record.basic = True
        if children and not record.children:
            for child in items:
                self._node(child, scope, children=False)
            record.children = True  # Never promote after a failed child check.
        # Child visits can evict the parent. Re-admit this same record through
        # the one bounded cache; validation state has no separate retention.
        self._remember(("object", root["digest"], root["kind"]), record, record.cache_size)
        return body

    @staticmethod
    def _route(children, key):
        return max(0, bisect_right([tuple(child["first"]) for child in children], key) - 1)

    def _get(self, root, scope, key):
        body = self._node(root, scope)
        if root["kind"] == "range-cell":
            item = next((item for item in body["entries"] if tuple(item["key"]) == key), None)
            return None if item is None else self._decode(item["value"])
        return self._get(body["children"][self._route(body["children"], key)], scope, key)

    def _items(self, root, scope, after=None):
        body = self._node(root, scope)
        if root["kind"] == "range-cell":
            for item in body["entries"]:
                if after is None or tuple(item["key"]) > after:
                    yield tuple(item["key"]), self._decode(item["value"])
        else:
            for child in body["children"]:
                if after is None or tuple(child["last"]) > after:
                    yield from self._items(child, scope, after)

    def _seek(self, root, scope, key, *, reverse=False, strict=False):
        """Authenticated floor/lower bound; never restart an entry iterator."""
        body = self._node(root, scope)
        if root["kind"] == "range-cell":
            entries = body["entries"]
            keys = [tuple(item["key"]) for item in entries]
            if reverse:
                ordinal = (bisect_left(keys, key) if strict else bisect_right(keys, key)) - 1
            else:
                ordinal = bisect_right(keys, key) if strict else bisect_left(keys, key)
            if 0 <= ordinal < len(entries):
                return keys[ordinal], self._decode(entries[ordinal]["value"])
            return None
        children = reversed(body["children"]) if reverse else body["children"]
        for child in children:
            fence = tuple(child["first"] if reverse else child["last"])
            eligible = ((fence < key if strict else fence <= key) if reverse
                        else (fence > key if strict else fence >= key))
            if eligible:
                found = self._seek(child, scope, key, reverse=reverse, strict=strict)
                if found is not None:
                    return found
        return None

    def _rebalance(self, scope, pages):
        result = []
        for page in pages:
            result.append(page)
            while len(result) > 1 and min(result[-2]["used_bytes"], result[-1]["used_bytes"]) < self.page_bytes // 3:
                page, other = result[-2:]
                if page["kind"] != other["kind"] or page["height"] != other["height"]:
                    raise self.integrity_error("owner content pages: sibling levels differ")
                field = "entries" if page["kind"] == "range-cell" else "children"
                repaired = self._pack(scope, page["kind"], self._node(page, scope)[field] + self._node(other, scope)[field])
                result[-2:] = repaired
                if len(repaired) > 1:
                    break  # An indivisible routing entry may limit occupancy.
        return result

    def _mutate_leaf_entries(self, entries, changes):
        # One mutation/preimage owner for ordinary and embedded leaves.
        values = {tuple(item["key"]): item["value"] for item in entries}
        for key, change in changes.items():
            previous = None if key not in values else self._decode(values[key])
            if callable(change):
                replacement = change(previous)
            else:
                expected, replacement = change
                if not _same_json(previous, expected):
                    raise self.integrity_error("owner content pages: replacement preimage differs")
            if replacement is None:
                values.pop(key, None)
            else:
                values[key] = self._encode(replacement)
        return [{"key": list(key), "value": value} for key, value in sorted(values.items())]

    def _update(self, root, scope, changes):
        if not changes:
            return [root]
        body = self._node(root, scope)
        if root["kind"] == "range-cell":
            items = self._mutate_leaf_entries(body["entries"], changes)
            return [root] if items == body["entries"] else self._pack(scope, "range-cell", items)
        children = body["children"]
        batches = [{} for _child in children]
        for key, change in changes.items():
            batches[self._route(children, key)][key] = change
        pages = [page for child, batch in zip(children, batches) for page in self._update(child, scope, batch)]
        return self._pack(scope, "range-branch", self._rebalance(scope, pages))

    def _position_payload(self, positions):
        exact = {"positions": positions}
        raw = canonical_json_bytes(exact)
        if len(raw) <= self.page_bytes // 3:
            return None
        if positions:
            runs = []
            for previous, position in pairwise(positions):
                gap = position - previous
                if runs and runs[-1][0] == gap:
                    runs[-1][1] += 1
                else:
                    runs.append([gap, 1])
            compact = {"first": positions[0], "count": len(positions), "gaps": runs}
            encoded = canonical_json_bytes(compact)
            # This compares complete vector payloads, not envelopes, graph
            # shape or total storage. Equality deliberately retains exact.
            if len(encoded) < len(raw):
                return compact, encoded
        return exact, raw

    @staticmethod
    def _expand_position_runs(first, runs):
        positions = [first]
        for gap, count in runs:
            positions.extend(first + gap * ordinal for ordinal in range(1, count + 1))
            first += gap * count
        return positions

    def _vector_positions(self, decoded, scope, field_length):
        if not isinstance(decoded, dict):
            raise self.integrity_error("owner content pages: position vector payload differs")
        if set(decoded) == {"positions"}:
            return decoded["positions"]
        if set(decoded) != {"first", "count", "gaps"}:
            raise self.integrity_error("owner content pages: position vector payload differs")
        first, count, runs = decoded["first"], decoded["count"], decoded["gaps"]
        # Every shape, count, run and field bound is checked before allocation
        # proportional to the decoded count. No scientific field-size cap.
        if (scope[0] != "lexical" or not index_module._integer(field_length, len(scope[-1]))
                or not index_module._integer(first) or not index_module._integer(count, 1)
                or count > field_length - len(scope[-1]) + 1 or not isinstance(runs, list)):
            raise self.integrity_error("owner content pages: compact positions exceed their field")
        total, last, previous_gap = 1, first, None
        for run in runs:
            if (not isinstance(run, list) or len(run) != 2
                    or any(not index_module._integer(item, 1) for item in run)
                    or run[0] == previous_gap):
                raise self.integrity_error("owner content pages: compact position runs are noncanonical")
            gap, repetitions = run
            total += repetitions
            last += gap * repetitions
            if total > count or last + len(scope[-1]) > field_length:
                raise self.integrity_error("owner content pages: compact positions exceed their field")
            previous_gap = gap
        if total != count or last + len(scope[-1]) > field_length:
            raise self.integrity_error("owner content pages: compact position count or bound differs")
        return self._expand_position_runs(first, runs)

    def _factor(self, value):
        source = {name: value[name] for name in _SOURCE_FIELDS}
        raw = canonical_json_bytes(source)
        key = ("source-write", raw)
        digest = self._cached(key)
        if digest is None:
            encoded = self._encode(source)
            references = (encoded["overflow"]["digest"],) if "overflow" in encoded else ()
            digest = self._write("range-source", references, value=encoded)
            self._remember(key, digest, len(raw) + len(digest))
        payload = self._position_payload(value["positions"])
        if payload is None:
            return {"source": digest, "positions": value["positions"], "field_length": value["field_length"]}
        vector, vector_raw = payload
        vector_key = ("position-write", vector_raw)
        vector_digest = self._cached(vector_key)
        if vector_digest is None:
            encoded = self._encode(vector)
            references = (encoded["overflow"]["digest"],) if "overflow" in encoded else ()
            vector_digest = self._write("range-position-vector", references, value=encoded)
            self._remember(vector_key, vector_digest, len(vector_raw) + len(vector_digest))
        # Length belongs to this qualified field occurrence, not the reusable
        # vector: unchanged positions may survive an append in a new revision.
        return {"source": digest, "position_vector": vector_digest, "field_length": value["field_length"]}

    def _posting(self, scope, key, value):
        external = "position_vector" in value
        position_field = "position_vector" if external else "positions"
        if set(value) != {"source", position_field, "field_length"}:
            raise self.integrity_error("owner content pages: range membership shape differs")
        source = self._load(value["source"], "range-source")
        if set(source) != {"domain", "profile", "kind", "value"}:
            raise self.integrity_error("owner content pages: range source shape differs")
        if external:
            if not index_module._digest(value["position_vector"]):
                raise self.integrity_error("owner content pages: position vector reference differs")
            vector = self._load(value["position_vector"], "range-position-vector")
            if set(vector) != {"domain", "profile", "kind", "value"}:
                raise self.integrity_error("owner content pages: position vector shape differs")
            decoded = self._decode(vector["value"])
            positions = self._vector_positions(decoded, scope, value["field_length"])
        else:
            positions = value["positions"]
        # The reader validates representation grammar and scientific meaning,
        # not whether this authenticated value was the writer's cheapest one.
        # Re-running the size chooser would re-encode every absolute vector.
        return self._validated_posting(key, {
            **self._decode(source["value"]), "positions": positions, "field_length": value["field_length"],
        }, scope)

    def _validated_posting(self, key, value, scope):
        # Scope arguments are validated at public entry points. These values
        # came from the authenticated graph, so malformed logical material is
        # an integrity failure under the Store's supplied exception boundary.
        try:
            return index_module._posting_from_value(key, value, scope)
        except ValueError as exc:
            raise self.integrity_error(str(exc)) from exc

    def posting(self, root, directory, key):
        with self._operation():
            index_module._validate_directory_key(directory)
            subtree = self._get(root, (), directory)
            value = None if subtree is None else self._posting_get(subtree, directory, key)
            return None if value is None else self._posting(directory, key, value)

    def postings(self, root, directory, after=None):
        with self._operation():
            index_module._validate_directory_key(directory)
            subtree = self._get(root, (), directory)
            if subtree is not None:
                for key, value in self._posting_items(subtree, directory, after):
                    yield self._posting(directory, key, value)

    def iter_entries(self, root):
        with self._operation():
            for directory, subtree in self._items(root, ()):
                index_module._validate_directory_key(directory)
                for key, value in self._posting_items(subtree, directory):
                    yield directory, self._posting(directory, key, value)

    def replace_source(self, root, removals, additions):
        with self._operation():
            changes = {}
            for removing, entries in ((True, removals), (False, additions)):
                for directory, posting in entries:
                    index_module._validate_directory_key(directory)
                    value = posting.to_mapping()
                    index_module._validate_posting_value(posting.key, value, directory)
                    group = changes.setdefault(directory, {})
                    if removing:
                        if posting.key in group:
                            raise self.integrity_error("owner content pages: duplicate removal")
                        group[posting.key] = (self._factor(value), None)
                    else:
                        before, pending = group.get(posting.key, (None, None))
                        if pending is not None:
                            raise self.integrity_error("owner content pages: duplicate addition")
                        group[posting.key] = (before, self._factor(value))

            def replace_directory(previous, directory, delta):
                before = previous if previous is not None else self._tree(directory, [])
                after = self._finish(directory, self._update(before, directory, delta))
                return after if after["count"] else None

            batch = {directory: (lambda previous, d=directory, delta=delta: replace_directory(previous, d, delta))
                     for directory, delta in changes.items()}
            return self._commit(self._finish((), self._update(root, (), batch)))



class OccurrenceIndex(PageTree):
    """Qualified occurrence/field-map index using the single page owner.

    Source input and audit input are independently supplied by the Store.
    Current heads and retained presence events remain variant-partitioned.
    This module has no source custody, schema or migration authority.
    """

    _ROOT = ("occurrence-experiment",)
    _NAMES = ("current", "events", "heads", "occurrences", "ordinary")

    def __init__(self, packed, *, page_bytes=4096, compact_page_keys=True,
                 cache_items_limit=128, cache_bytes_limit=2 * 1024 * 1024):
        super().__init__(packed, page_bytes=page_bytes, compact_page_keys=compact_page_keys,
                         cache_items_limit=cache_items_limit, cache_bytes_limit=cache_bytes_limit)

    def empty_root(self, *, persist=False):
        """Derive the empty commitment with the same writer, without scratch.

        Pure derivation needs no database. Persistence requires the caller's
        transaction and publishes exactly the graph committed by build(()).
        """
        if type(persist) is not bool:
            raise TypeError("persist must be boolean")
        if self._depth or self._pending or self._bootstrap_scratch is not None:
            raise self.integrity_error("owner content pages: empty root requires its own operation")
        if persist and (self.packed.connection is None or not self.packed.connection.in_transaction):
            raise self.integrity_error("owner content pages: bootstrap requires the caller transaction")
        with self._operation():
            roots = {name: self._tree(self._scope(name), ()) for name in self._NAMES}
            root = self._tree(self._ROOT, (((name,), roots[name]) for name in self._NAMES))
            return super()._commit(root) if persist else root

    def _scope(self, name):
        return () if name == "ordinary" else self._ROOT + (name,)

    def _value_references(self, scope, key, value):
        if scope == self._ROOT:
            if key not in tuple((name,) for name in self._NAMES):
                raise self.integrity_error("owner content pages: occurrence root name differs")
            return [value["digest"]]
        if scope == self._scope("occurrences"):
            return [value["source"], value["map"]["digest"]]
        if scope[:1] == ("occurrence-field-map",) or scope in (
                self._scope("current"), self._scope("events"), self._scope("heads")):
            return []
        return super()._value_references(scope, key, value)

    def _commit(self, root):
        # The unchanged nonlexical writer runs nested in this operation. Its
        # pages join the final root's reachability; no separate publication.
        return root if self._depth > 1 else super()._commit(root)

    def _roots(self, root):
        values = dict(self._items(root, self._ROOT))
        if set(values) != {(name,) for name in self._NAMES}:
            raise self.integrity_error("owner content pages: occurrence manifest differs")
        return {name: values[(name,)] for name in self._NAMES}

    @staticmethod
    def _owner(directory, key):
        return (directory[1], directory[2], directory[4], directory[5], key[1])

    @staticmethod
    def _map_scope(owner):
        return ("occurrence-field-map",) + owner

    @staticmethod
    def _inverse(directory):
        return directory[1:3] + directory[4:]

    @staticmethod
    def _source_value(value, *, inventory=False):
        source = {name: value[name] for name in _SOURCE_FIELDS}
        if inventory:
            source["metadata"] = {name: item for name, item in source["metadata"].items()
                                  if name != "capture_descriptor"}
        return source

    def _source_groups(self, collections):
        from research_core.owner_content_access import SourceProjection, source_descriptor_entries

        groups, ordinary, seen = {}, [], set()
        for selected, sources in collections:
            for source in sources:
                if not isinstance(source, SourceProjection):
                    raise self.integrity_error("owner content pages: update requires source projections")
                # Presence is derived once per SOURCE, never once per variant.
                for directory, posting in source_descriptor_entries(source, selected):
                    key = (directory, posting.key)
                    if key in seen:
                        raise self.integrity_error("owner content pages: duplicate source membership")
                    seen.add(key)
                    ordinary.append((directory, posting))
                    if directory[0] != "owner_inventory":
                        continue
                    identity = self._owner(directory, posting.key) + (posting.key[2],)
                    value = posting.to_mapping()
                    groups[(identity, selected)] = SimpleNamespace(
                        inventory=posting, source=self._source_value(value, inventory=True),
                        fields={name: text.casefold() for name, text in source.fields[directory[4]].items()})
        return groups, ordinary

    def _binding(self, value, identity, *, authenticate=True):
        if (len(identity) != 6 or any(not isinstance(part, str) or not part for part in identity[:-1])
                or not index_module._integer(identity[-1])
                or not isinstance(value, dict)
                or set(value) != {"source", "map", "lengths", "current", "retained"}
                or not index_module._digest(value["source"])
                or not isinstance(value["map"], dict) or set(value["map"]) != self._SUMMARY
                or type(value["current"]) is not bool or type(value["retained"]) is not bool
                or not (value["current"] or value["retained"])
                or not isinstance(value["lengths"], dict)
                or any(field not in index_module.SEARCH_FIELDS or not index_module._integer(length, 1)
                       for field, length in value["lengths"].items())):
            raise self.integrity_error("owner content pages: occurrence binding differs")
        if not authenticate:
            return None  # Seek reads the authenticated binding, not its bodies.
        self._node(value["map"], self._map_scope(identity[:-1]))
        source = self._load(value["source"], "range-source")
        if set(source) != {"domain", "profile", "kind", "value"}:
            raise self.integrity_error("owner content pages: occurrence source shape differs")
        return self._decode(source["value"])

    def _map_values(self, value, owner):
        if value is None:
            return {}
        root, scope = value["map"], self._map_scope(owner)
        # A warm derived map never validates a newly supplied summary/scope.
        # Its prior COMPLETE traversal is reusable only inside this operation,
        # in the SAME bounded LRU as authenticated bodies/overflow values.
        self._node(root, scope)
        key = ("occurrence-map", scope, root["digest"])
        values = self._cached(key)
        if values is None:
            values = MappingProxyType(dict(self._items(root, scope)))
            size = len(canonical_json_bytes([scope, root, list(values.items())]))
            self._remember(key, values, size)
        return values

    def _prepared_occurrence(self, identity, value):
        # This record belongs to the current call frame, not another cache or
        # authority. Full maps are admitted (or refused) by _map_values above.
        return SimpleNamespace(identity=identity, value=value, source=self._binding(value, identity),
                               facts=self._map_values(value, identity[:-1]))

    def _logical_scopes(self, prepared, selections):
        identity, value, source = prepared.identity, prepared.value, prepared.source
        owner = identity[:-1]
        for (field, gram), payload in prepared.facts.items():
            directory = ("lexical", owner[0], owner[1], selections[0], owner[2], owner[3], field, gram)
            index_module._validate_directory_key(directory)
            if field not in value["lengths"]:
                raise self.integrity_error("owner content pages: field lacks its occurrence length")
            positions = self._vector_positions(payload, directory, value["lengths"][field])
            for selected in selections:
                scoped = directory[:3] + (selected,) + directory[4:]
                index_module._validate_directory_key(scoped)
                # Keep full validation and detached metadata for EACH outward
                # Posting/scope; only the authenticated map and expansion are
                # shared. A consumer cannot mutate another scope's metadata.
                yield scoped, self._validated_posting((owner[3], owner[4], identity[-1]),
                    {**source, "positions": positions, "field_length": value["lengths"][field]}, scoped)

    def _inventory_at(self, roots, prepared, selected):
        identity, owner = prepared.identity, prepared.identity[:-1]
        inventory = ("owner_inventory", owner[0], owner[1], selected, owner[2], owner[3])
        posting = super().posting(roots["ordinary"], inventory, (owner[3], owner[4], identity[-1]))
        if posting is None:
            raise self.integrity_error("owner content pages: occurrence inventory is absent")
        # The ordinary reader already performed the full inventory validator.
        # Every lexical Posting below derives from this exact bound source;
        # comparing it once replaces reserializing that source for every gram.
        if not _same_json(
                self._source_value(posting.to_mapping(), inventory=True), prepared.source):
            raise self.integrity_error("owner content pages: source group mixes qualifications")
        return inventory, posting

    def _assert_source_fields(self, prepared, fields, selected, *, reason="source audit"):
        """Fresh source derivation shared by preimages and independent audit.

        Callers supply their own source fields. This checks logical equality,
        not writer-optimal encoding: authenticated absolute and compact vectors
        may represent the same positions. No outward lexical Posting is needed.
        """
        identity, binding = prepared.identity, prepared.value
        expected_lengths, lexical_count = {}, 0
        for field, length, grams in index_module._field_gram_positions(fields):
            if grams:
                expected_lengths[field] = length
            for gram, wanted in grams.items():
                directory = ("lexical", identity[0], identity[1], selected, identity[2], identity[3], field, gram)
                index_module._validate_directory_key(directory)
                payload = prepared.facts.get((field, gram))
                if payload is None:
                    raise self.integrity_error(f"owner content pages: {reason} omitted a source gram")
                positions = self._vector_positions(payload, directory, length)
                # Equality to independently derived valid offsets also checks
                # the absolute codec's types, strict order and field bounds.
                if (not isinstance(positions, list) or len(positions) != len(wanted)
                        or any(type(found) is not int or found != required for found, required in zip(positions, wanted))):
                    raise self.integrity_error(f"owner content pages: {reason} positions differ")
                lexical_count += 1
            positions = wanted = payload = None
            del grams
        if (lexical_count != len(prepared.facts)
                or canonical_json_bytes(binding["lengths"]) != canonical_json_bytes(expected_lengths)):
            raise self.integrity_error(f"owner content pages: {reason} field coverage differs")
        return lexical_count

    def _edit(self, root, scope, changes):
        return self._finish(scope, self._update(root, scope, changes)) if changes else root

    def build(self, inventory):
        """Bootstrap directly from caller-qualified source projections.

        Disk rows coalesce exact projected occurrences, not expanded lexical
        Postings. The independent audit rederives its own source relation.
        """
        from research_core.owner_content_access import SourceProjection, source_descriptor_entries

        if not self.packed.connection.in_transaction:
            raise self.integrity_error("owner content pages: bootstrap requires the caller transaction")
        if self._depth or self._pending or self._bootstrap_scratch is not None:
            raise self.integrity_error("owner content pages: bootstrap requires its own operation")
        self.bootstrap_buffers = {"source_groups": 0, "source_group_logical_memberships_peak": 0,
            "source_input_encoded_bytes_peak": 0, "tail_payload_bytes_peak": 0,
            "tail_entries_peak": 0, "level_rows_peak": 0, "pending_objects_peak": 0,
            "logical_memberships": 0}
        source_visits = source_groups = 0
        role_counts = {name: 0 for name in (*self._NAMES, "ordinary-directory")}
        completed_rows = {name: 0 for name in ("index_ordinary_build", "index_inverse_build", "index_directory_build")}

        def tracked_rows(values, phase, total):
            for value in values:
                yield value
                completed_rows[phase] += 1
                if completed_rows[phase] % 256 == 0:
                    report_preparation_progress(phase, completed_rows[phase], total)

        report_preparation_progress("index_source_ingest")
        with index_module._SortedEntries((), integrity_error=self.integrity_error) as ordered:
            scratch, created = ordered._database(), []
            try:
                definitions = {
                    "occurrence_bootstrap_input": "identity_sort BLOB PRIMARY KEY, identity_json TEXT, source_json TEXT, fields_json TEXT, inventory_json TEXT, is_current INTEGER, is_retained INTEGER",
                    "occurrence_bootstrap_rows": "role TEXT, scope_sort BLOB, key_sort BLOB, scope_json TEXT, key_json TEXT, value_json TEXT, PRIMARY KEY(role,scope_sort,key_sort)",
                    "occurrence_bootstrap_levels": "job INTEGER, level INTEGER, ordinal INTEGER, body TEXT, PRIMARY KEY(job,level,ordinal)",
                }
                for name, columns in definitions.items():
                    scratch.execute(f"CREATE TABLE {name} ({columns}) WITHOUT ROWID")
                    created.append(name)
                with self._operation():
                    self._bootstrap_scratch = scratch
                    # Arbitrary source order becomes typed owner/variant/
                    # occurrence order on disk. Presence is once per SOURCE.
                    for selected, source in inventory:
                        if not isinstance(source, SourceProjection):
                            raise self.integrity_error("owner content pages: bootstrap requires source projections")
                        for directory, posting in source_descriptor_entries(source, selected):
                            value = posting.to_mapping()
                            role_counts["ordinary"] += self._bootstrap_row("ordinary", directory, posting.key, self._factor(value))
                            if directory[0] != "owner_inventory":
                                continue
                            identity = self._owner(directory, posting.key) + (posting.key[2],)
                            identity_sort = index_module._tuple_sort_key(identity)
                            source_json = canonical_json_bytes(self._source_value(value, inventory=True)).decode("utf-8")
                            fields_json = canonical_json_bytes({name: text.casefold()
                                for name, text in source.fields[directory[4]].items()}).decode("utf-8")
                            inventory_json = canonical_json_bytes(value).decode("utf-8")
                            flags = (int(selected == "current_at_cut"), int(selected == "retained_history"))
                            previous = scratch.execute("SELECT source_json,fields_json,inventory_json FROM occurrence_bootstrap_input "
                                "WHERE identity_sort=?", (identity_sort,)).fetchone()
                            if previous is None:
                                scratch.execute("INSERT INTO occurrence_bootstrap_input VALUES (?,?,?,?,?,?,?)",
                                    (identity_sort, canonical_json_bytes(identity).decode("utf-8"),
                                     source_json, fields_json, inventory_json, *flags))
                                source_groups += 1
                            elif previous != (source_json, fields_json, inventory_json):
                                raise self.integrity_error("owner content pages: source occurrence qualifications differ")
                            else:
                                # Same-scope equal inputs coalesce, just as the
                                # old sorted logical relation did. Conflicting
                                # duplicates never overwrite their preimage.
                                scratch.execute("UPDATE occurrence_bootstrap_input SET is_current=MAX(is_current,?),"
                                    "is_retained=MAX(is_retained,?) WHERE identity_sort=?", (*flags, identity_sort))
                        source_visits += 1
                        report_preparation_progress("index_source_ingest", source_visits)
                    report_preparation_progress("index_source_ingest", source_visits, source_visits)
                    self.bootstrap_buffers["logical_memberships"] = scratch.execute(
                        "SELECT COUNT(*) FROM occurrence_bootstrap_rows WHERE role='ordinary'").fetchone()[0]
                    source = posting = value = source_json = fields_json = inventory_json = previous = None
                    rows = self._bootstrap_select("SELECT identity_json,source_json,fields_json,inventory_json,is_current,is_retained "
                        "FROM occurrence_bootstrap_input ORDER BY identity_sort")
                    prior_owner, prior_presence, current_seen = None, set(), False
                    report_preparation_progress("index_occurrence_build", 0, source_groups)
                    for identity_raw, source_raw, fields_raw, inventory_raw, current, retained in rows:
                        identity = tuple(index_module.loads_strict_json(identity_raw))
                        owner = identity[:-1]
                        if owner != prior_owner:
                            prior_owner, prior_presence, current_seen = owner, set(), False
                        source_value = index_module.loads_strict_json_object(source_raw)
                        source_digest = self._factor({**source_value, "positions": [], "field_length": None})["source"]
                        fields = index_module.loads_strict_json_object(fields_raw)
                        lengths, facts = {}, {}
                        for field, length, grams in index_module._field_gram_positions(fields):
                            if grams:
                                lengths[field] = length
                            for gram, absolute in grams.items():
                                index_module._validate_directory_key(("lexical", owner[0], owner[1],
                                    "current_at_cut" if current else "retained_history", owner[2], owner[3], field, gram))
                                payload = self._position_payload(absolute)
                                facts[(field, gram)] = {"positions": absolute} if payload is None else payload[0]
                            absolute = payload = None
                            del grams
                        count = (len(facts) + 1) * (current + retained)
                        self.bootstrap_buffers["source_groups"] += 1
                        self.bootstrap_buffers["source_group_logical_memberships_peak"] = max(
                            self.bootstrap_buffers["source_group_logical_memberships_peak"], count)
                        self.bootstrap_buffers["source_input_encoded_bytes_peak"] = max(
                            self.bootstrap_buffers["source_input_encoded_bytes_peak"],
                            sum(len(raw.encode("utf-8")) for raw in (identity_raw, source_raw, fields_raw, inventory_raw)))
                        self.bootstrap_buffers["logical_memberships"] += len(facts) * (current + retained)
                        map_root = self._tree(self._map_scope(owner), sorted(facts.items()))
                        binding = {"source": source_digest, "map": map_root, "lengths": lengths,
                                   "current": bool(current), "retained": bool(retained)}
                        role_counts["occurrences"] += self._bootstrap_row("occurrences", self._scope("occurrences"), identity, binding)
                        if current:
                            if current_seen:
                                raise self.integrity_error("owner content pages: owner has multiple current heads")
                            current_seen = True
                            role_counts["heads"] += self._bootstrap_row("heads", self._scope("heads"), owner, {"revision": identity[-1]})
                            for field, gram in facts:
                                role_counts["current"] += self._bootstrap_row("current", self._scope("current"),
                                    owner[:4] + (field, gram, owner[4]), {"present": True})
                        if retained:
                            for field, gram in prior_presence ^ facts.keys():
                                role_counts["events"] += self._bootstrap_row("events", self._scope("events"),
                                    owner[:4] + (field, gram, owner[4], identity[-1]), {"present": (field, gram) in facts})
                            prior_presence = set(facts)
                        del source_value, fields, lengths, facts, absolute, payload, binding
                        report_preparation_progress("index_occurrence_build", self.bootstrap_buffers["source_groups"], source_groups)
                    # Ordinary inventory/presence uses the same physical
                    # directory/posting codec, exact keys and source factoring.
                    report_preparation_progress("index_ordinary_build", 0, role_counts["ordinary"])
                    for (raw_scope,) in self._bootstrap_select("SELECT scope_json FROM occurrence_bootstrap_rows "
                            "WHERE role='ordinary' GROUP BY scope_sort ORDER BY scope_sort"):
                        directory = tuple(index_module.loads_strict_json(raw_scope))
                        subtree = self._tree(directory, tracked_rows(self._bootstrap_values("ordinary", directory),
                            "index_ordinary_build", role_counts["ordinary"]))
                        role_counts["ordinary-directory"] += self._bootstrap_row("ordinary-directory", (), directory, subtree)
                    report_preparation_progress("index_ordinary_build", completed_rows["index_ordinary_build"], role_counts["ordinary"])
                    inverse_total = sum(role_counts[name] for name in self._NAMES if name != "ordinary")
                    report_preparation_progress("index_inverse_build", 0, inverse_total)
                    roots = {}
                    for name in self._NAMES:
                        if name == "ordinary":
                            continue
                        roots[name] = self._tree(self._scope(name), tracked_rows(
                            self._bootstrap_values(name, self._scope(name)), "index_inverse_build", inverse_total))
                    report_preparation_progress("index_inverse_build", completed_rows["index_inverse_build"], inverse_total)
                    directory_total = role_counts["ordinary-directory"] + len(self._NAMES)
                    report_preparation_progress("index_directory_build", 0, directory_total)
                    roots["ordinary"] = self._tree((), tracked_rows(self._bootstrap_values("ordinary-directory", ()),
                        "index_directory_build", directory_total))
                    root = self._tree(self._ROOT, tracked_rows((((name,), roots[name]) for name in self._NAMES),
                        "index_directory_build", directory_total))
                    report_preparation_progress("index_directory_build", completed_rows["index_directory_build"], directory_total)
                    return root
            finally:
                # A rejected page/group can leave a SELECT suspended. Close
                # it before dropping tables; the outer owner removes scratch.
                for cursor in tuple(self._bootstrap_cursors):
                    cursor.close()
                self._bootstrap_cursors.clear()
                self._bootstrap_scratch = None
                for name in reversed(created):
                    scratch.execute(f"DROP TABLE {name}")

    def _bootstrap_row(self, role, scope, key, value):
        encoded = canonical_json_bytes(value).decode("utf-8")
        scratch = self._bootstrap_scratch
        identity = (role, index_module._tuple_sort_key(scope), index_module._tuple_sort_key(key))
        inserted = scratch.execute("INSERT INTO occurrence_bootstrap_rows VALUES (?,?,?,?,?,?) ON CONFLICT DO NOTHING",
            (*identity, canonical_json_bytes(scope).decode("utf-8"), canonical_json_bytes(key).decode("utf-8"), encoded)).rowcount
        if not inserted:
            previous = scratch.execute("SELECT value_json FROM occurrence_bootstrap_rows WHERE role=? AND scope_sort=? AND key_sort=?",
                                       identity).fetchone()
            if previous is None or previous[0] != encoded:
                raise self.integrity_error("owner content pages: bootstrap physical membership collision")
        return int(bool(inserted))

    def _bootstrap_values(self, role, scope):
        for key, value in self._bootstrap_select("SELECT key_json,value_json FROM occurrence_bootstrap_rows "
                "WHERE role=? AND scope_sort=? ORDER BY key_sort", (role, index_module._tuple_sort_key(scope))):
            yield tuple(index_module.loads_strict_json(key)), index_module.loads_strict_json_object(value)

    def _bootstrap_select(self, sql, parameters=()):
        with closing(self._bootstrap_scratch.execute(sql, parameters)) as cursor:
            self._bootstrap_cursors.add(cursor)
            try:
                yield from cursor
            finally:
                self._bootstrap_cursors.discard(cursor)

    def _retained_neighbor(self, roots, identity, *, reverse=False, strict=False):
        found = self._seek(roots["occurrences"], self._scope("occurrences"), identity,
                           reverse=reverse, strict=strict)
        while found is not None and found[0][:-1] == identity[:-1]:
            self._binding(found[1], found[0], authenticate=False)
            if found[1]["retained"]:
                return found
            found = self._seek(roots["occurrences"], self._scope("occurrences"), found[0],
                               reverse=reverse, strict=True)
        return None

    def replace_source(self, root, delta):
        from research_core.owner_content_access import OwnerContentDelta

        if not isinstance(delta, OwnerContentDelta):
            raise self.integrity_error("owner content pages: update requires OwnerContentDelta")
        if delta.pending_capture_projection:
            raise self.integrity_error("owner content pages: update has pending Capture projection")
        removed, removals = self._source_groups((("current_at_cut", delta.removed_current),
                                                ("retained_history", delta.removed_retained)))
        added, additions = self._source_groups((("retained_history", delta.added_retained),
                                               ("current_at_cut", delta.added_current)))
        with self._operation():
            roots = self._roots(root)
            changes, affected = {}, {}
            for identity, selected in removed.keys() | added.keys():
                affected.setdefault(identity, {})[selected] = added.get((identity, selected))
            old_bindings, targets = {}, {}
            replayed_sources, replayed_current_owners = set(), set()
            for identity, replacement in sorted(affected.items()):
                old = self._get(roots["occurrences"], self._scope("occurrences"), identity)
                old_bindings[identity] = old
                prepared = None if old is None else self._prepared_occurrence(identity, old)
                checked_fields = None

                def assert_group(group, selected, reason):
                    nonlocal checked_fields
                    flag = "current" if selected == "current_at_cut" else "retained"
                    if prepared is None or not old[flag]:
                        raise self.integrity_error(f"owner content pages: {reason} is absent")
                    _directory, inventory = self._inventory_at(roots, prepared, selected)
                    if inventory._canonical_bytes() != group.inventory._canonical_bytes():
                        raise self.integrity_error(f"owner content pages: {reason} qualifications differ")
                    # Same exact map/source may serve both selections. Each
                    # inventory is still authenticated, including Capture's
                    # full descriptor; only equal source-field work is reused.
                    if checked_fields is None or checked_fields != group.fields:
                        self._assert_source_fields(prepared, group.fields, selected, reason=reason)
                        checked_fields = group.fields

                flags = {}
                for selected, flag in (("current_at_cut", "current"), ("retained_history", "retained")):
                    key = identity, selected
                    if key in removed:
                        assert_group(removed[key], selected, "occurrence removal preimage")
                    elif key in added and old is not None and old[flag]:
                        assert_group(added[key], selected, "occurrence addition collision")
                        replayed_sources.add(key)
                        if selected == "current_at_cut":
                            replayed_current_owners.add(identity[:2] + identity[3:5])
                    flags[flag] = (replacement[selected] is not None if selected in replacement
                                   else bool(old is not None and old[flag]))
                incoming = [group for selected, group in replacement.items() if group is not None]
                chosen = incoming[0] if incoming else None
                if chosen is not None:
                    if any(group.fields != chosen.fields or group.inventory._canonical_bytes() != chosen.inventory._canonical_bytes()
                           for group in incoming[1:]):
                        raise self.integrity_error("owner content pages: current/retained occurrence qualifications differ")
                    for selected, flag in (("current_at_cut", "current"), ("retained_history", "retained")):
                        if selected not in replacement and flags[flag]:
                            assert_group(chosen, selected, "current/retained occurrence qualifications")
                else:
                    for selected, flag in (("current_at_cut", "current"), ("retained_history", "retained")):
                        if flags[flag]:
                            self._inventory_at(roots, prepared, selected)
                targets[identity] = chosen, flags
                del prepared, checked_fields
            # The source owner above admits an equal existing addition as a
            # replay. Preserve the ordinary writer's strict insertion rule by
            # omitting only individually authenticated, equal memberships.
            # Capture selectors and field-presence rows need their own exact
            # checks; inventory equality alone does not stand in for them.
            # New source/scope additions never need these replay lookups.
            removed_memberships = {(directory, posting.key) for directory, posting in removals}
            ordinary_additions = []
            for directory, posting in additions:
                if directory[0] == "current_field_presence":
                    replayed = directory[1:3] + posting.key in replayed_current_owners
                else:
                    replayed = (self._owner(directory, posting.key) + (posting.key[2],),
                                directory[3]) in replayed_sources
                if replayed and (directory, posting.key) not in removed_memberships:
                    previous = super().posting(roots["ordinary"], directory, posting.key)
                    if previous is not None and previous._canonical_bytes() == posting._canonical_bytes():
                        continue
                ordinary_additions.append((directory, posting))
            # Every supplied preimage and surviving scope agreement is checked
            # before staging replacement bindings. No lexical Posting roundtrip
            # or retained-history scan stands between source and its field map.
            new_bindings, current_revisions_by_owner = {}, {}
            for identity, (chosen, flags) in sorted(targets.items()):
                old = old_bindings[identity]
                if not any(flags.values()):
                    new = None
                elif chosen is None:
                    new = {**old, **flags}
                else:
                    source_digest = self._factor({**chosen.source, "positions": [], "field_length": None})["source"]
                    facts, lengths = {}, {}
                    owner = identity[:-1]
                    for field, length, grams in index_module._field_gram_positions(chosen.fields):
                        if grams:
                            lengths[field] = length
                        for gram, absolute in grams.items():
                            index_module._validate_directory_key(("lexical", owner[0], owner[1],
                                "current_at_cut" if flags["current"] else "retained_history", owner[2], owner[3], field, gram))
                            payload = self._position_payload(absolute)
                            facts[(field, gram)] = {"positions": absolute} if payload is None else payload[0]
                        absolute = payload = None
                        del grams
                    previous = self._map_values(old, owner)
                    # Reuse an adjacent actual occurrence's owner-local map for
                    # a new revision. No revision-number arithmetic or free map.
                    anchor = old
                    if anchor is None:
                        neighbor = self._seek(roots["occurrences"], self._scope("occurrences"), identity,
                                              reverse=True, strict=True)
                        if neighbor is None or neighbor[0][:-1] != owner:
                            neighbor = self._seek(roots["occurrences"], self._scope("occurrences"), identity)
                        if neighbor is not None and neighbor[0][:-1] == owner:
                            anchor = neighbor[1]
                            self._binding(anchor, neighbor[0])
                            previous = self._map_values(anchor, owner)
                    map_root = self._tree(self._map_scope(owner), []) if anchor is None else anchor["map"]
                    delta = {key: (previous.get(key), facts.get(key)) for key in previous.keys() | facts.keys()
                             if previous.get(key) != facts.get(key)}
                    map_root = self._edit(map_root, self._map_scope(owner), delta)
                    new = {"source": source_digest, "map": map_root, "lengths": lengths, **flags}
                    del facts, previous
                new_bindings[identity] = new
                if new is not None and new["current"]:
                    current_revisions_by_owner.setdefault(identity[:-1], []).append(identity[-1])
                if old != new:
                    changes[identity] = (old, new)
            updated = dict(roots)
            updated["occurrences"] = self._edit(roots["occurrences"], self._scope("occurrences"), changes)
            head_changes, current_changes = {}, {}
            for owner in sorted({identity[:-1] for identity in affected}):
                old_head = self._get(roots["heads"], self._scope("heads"), owner)
                revision = None if old_head is None else old_head["revision"]
                if owner + (revision,) in new_bindings:
                    new = new_bindings[owner + (revision,)]
                    if new is None or not new["current"]:
                        revision = None
                # Keep the sorted occurrence order and conflict checks, but
                # inspect only this owner's candidates, not the whole batch.
                for candidate_revision in current_revisions_by_owner.get(owner, ()):
                    if revision is not None and revision != candidate_revision:
                        raise self.integrity_error("owner content pages: owner has multiple current heads")
                    revision = candidate_revision
                head = None if revision is None else {"revision": revision}
                if head != old_head:
                    head_changes[owner] = (old_head, head)
                old_value = None if old_head is None else self._get(
                    roots["occurrences"], self._scope("occurrences"), owner + (old_head["revision"],))
                new_value = None if head is None else self._get(
                    updated["occurrences"], self._scope("occurrences"), owner + (revision,))
                old_facts, new_facts = self._map_values(old_value, owner), self._map_values(new_value, owner)
                for field, gram in old_facts.keys() ^ new_facts.keys():
                    inverse = owner[:4] + (field, gram, owner[4])
                    current_changes[inverse] = ({"present": True} if (field, gram) in old_facts else None,
                                                {"present": True} if (field, gram) in new_facts else None)
            updated["heads"] = self._edit(roots["heads"], self._scope("heads"), head_changes)
            updated["current"] = self._edit(roots["current"], self._scope("current"), current_changes)
            # Only affected occurrence boundaries and their immediate retained
            # successors can change a canonical presence event. Metadata and
            # position-only differences do not themselves create new events.
            targets = {}
            for identity in affected:
                old, new = old_bindings[identity], new_bindings[identity]
                old_facts = self._map_values(old, identity[:-1]) if old is not None and old["retained"] else {}
                new_facts = self._map_values(new, identity[:-1]) if new is not None and new["retained"] else {}
                if old_facts.keys() == new_facts.keys() and bool(old and old["retained"]) == bool(new and new["retained"]):
                    continue
                targets.setdefault(identity, set()).update(old_facts.keys() | new_facts.keys())
                successor = self._retained_neighbor(updated, identity, strict=True)
                if successor is not None:
                    targets.setdefault(successor[0], set()).update(old_facts.keys() | new_facts.keys())
            event_changes = {}
            for identity, hints in sorted(targets.items()):
                previous = self._retained_neighbor(updated, identity, reverse=True, strict=True)
                old_previous = self._retained_neighbor(roots, identity, reverse=True, strict=True)
                current = self._get(updated["occurrences"], self._scope("occurrences"), identity)
                previous_facts = self._map_values(None if previous is None else previous[1], identity[:-1])
                old_previous_facts = self._map_values(None if old_previous is None else old_previous[1], identity[:-1])
                current_facts = self._map_values(current, identity[:-1]) if current is not None and current["retained"] else {}
                # An insertion can move an ABSENCE transition earlier: a gram
                # in the old predecessor but neither the inserted occurrence
                # nor its successor leaves a now-redundant false successor
                # event. Include old boundary support, not only new presence.
                # Fixed identity makes (field, gram) the varying event-key
                # prefix. Keep every preimage/absence read, in page order.
                for field, gram in sorted(hints | old_previous_facts.keys() | previous_facts.keys() | current_facts.keys()):
                    key = identity[:4] + (field, gram, identity[4], identity[5])
                    before = self._get(roots["events"], self._scope("events"), key)
                    present = (field, gram) in current_facts
                    after = ({"present": present} if current is not None and current["retained"]
                             and present != ((field, gram) in previous_facts) else None)
                    if before != after:
                        event_changes[key] = (before, after)
            updated["events"] = self._edit(roots["events"], self._scope("events"), event_changes)
            updated["ordinary"] = super().replace_source(roots["ordinary"], removals, ordinary_additions)
            delta = {(name,): (roots[name], updated[name]) for name in self._NAMES if roots[name] != updated[name]}
            result = self._edit(root, self._ROOT, delta)
            # Seal the combined graph, including an explicit caller's nested
            # update. The ordinary writer's intermediate commit stays deferred.
            return super()._commit(result)

    def _event(self, value):
        if not isinstance(value, dict) or set(value) != {"present"} or type(value["present"]) is not bool:
            raise self.integrity_error("owner content pages: presence event shape differs")
        return value["present"]

    def _seek_lexical(self, roots, directory, bound=None, *, strict=False):
        if bound is not None and bound[0] != directory[5]:
            if bound[0] > directory[5]:
                return None
            bound = None
        prefix = self._inverse(directory)
        owner_id = "" if bound is None else bound[1]
        revision = 0 if bound is None else bound[2]
        if directory[3] == "current_at_cut":
            found = self._seek(roots["current"], self._scope("current"), prefix + (owner_id,))
            while found is not None and found[0][:-1] == prefix:
                if not self._event(found[1]):
                    raise self.integrity_error("owner content pages: current membership is not positive")
                owner = prefix[:4] + (found[0][-1],)
                head = self._get(roots["heads"], self._scope("heads"), owner)
                if (head is None or set(head) != {"revision"} or not index_module._integer(head["revision"])):
                    raise self.integrity_error("owner content pages: current head is absent or malformed")
                key = (owner[3], owner[4], head["revision"])
                if bound is None or (key > bound if strict else key >= bound):
                    return key
                found = self._seek(roots["current"], self._scope("current"), found[0], strict=True)
            return None
        event_key = prefix + (owner_id, revision)
        event = self._seek(roots["events"], self._scope("events"), event_key, reverse=True)
        if event is None or event[0][:-1] != event_key[:-1]:
            event = self._seek(roots["events"], self._scope("events"), event_key)
        while event is not None and event[0][:6] == prefix:
            start, state = event
            following = self._seek(roots["events"], self._scope("events"), start, strict=True)
            end = following[0][-1] if following is not None and following[0][:-1] == start[:-1] else None
            if self._event(state):
                owner = prefix[:4] + (start[-2],)
                anchor_identity = owner + (start[-1],)
                anchor = self._get(roots["occurrences"], self._scope("occurrences"), anchor_identity)
                if anchor is not None:
                    self._binding(anchor, anchor_identity, authenticate=False)
                if anchor is None or not anchor["retained"]:
                    raise self.integrity_error("owner content pages: positive event occurrence is absent")
                same_owner = bound is not None and owner[4] == bound[1]
                lower = max(start[-1], bound[2]) if same_owner else start[-1]
                found = self._retained_neighbor(roots, owner + (lower,),
                    strict=bool(same_owner and strict and lower == bound[2]))
                if found is not None and (end is None or found[0][-1] < end):
                    return owner[3], owner[4], found[0][-1]
            event = following
        return None

    def _resolved_occurrence(self, roots, directory, key, *, required=False):
        owner = self._owner(directory, key)
        identity = owner + (key[2],)
        value = self._get(roots["occurrences"], self._scope("occurrences"), identity)
        flag = "current" if directory[3] == "current_at_cut" else "retained"
        if value is not None:
            self._binding(value, identity, authenticate=False)
        if value is None or not value[flag]:
            if required:
                raise self.integrity_error("owner content pages: inverse points at absent occurrence")
            return None
        return SimpleNamespace(identity=identity, value=value, source=self._binding(value, identity))

    def _joined_occurrence(self, prepared, directory, key, *, required=False):
        owner = self._owner(directory, key)
        if owner + (key[2],) != prepared.identity:
            raise self.integrity_error("owner content pages: prepared occurrence scope differs")
        value, source = prepared.value, prepared.source
        flag = "current" if directory[3] == "current_at_cut" else "retained"
        if not value[flag]:
            if required:
                raise self.integrity_error("owner content pages: inverse points at absent occurrence")
            return None
        payload = self._get(value["map"], self._map_scope(owner), directory[6:])
        if payload is None:
            if required:
                raise self.integrity_error("owner content pages: inverse disagrees with field map")
            return None
        if directory[6] not in value["lengths"]:
            raise self.integrity_error("owner content pages: field lacks its occurrence length")
        length = value["lengths"][directory[6]]
        return self._validated_posting(key, {**source,
            "positions": self._vector_positions(payload, directory, length), "field_length": length}, directory)

    def _joined(self, roots, directory, key, *, required=False):
        prepared = self._resolved_occurrence(roots, directory, key, required=required)
        return (None if prepared is None else
                self._joined_occurrence(prepared, directory, key, required=required))

    def posting(self, root, directory, key):
        with self._operation():
            index_module._validate_directory_key(directory)
            roots = self._roots(root)
            if directory[0] != "lexical":
                return super().posting(roots["ordinary"], directory, key)
            actual = self._seek_lexical(roots, directory, key)
            return self._joined(roots, directory, key, required=True) if actual == key else None

    def postings(self, root, directory, after=None):
        with self._operation():
            index_module._validate_directory_key(directory)
            roots = self._roots(root)
            if directory[0] != "lexical":
                yield from super().postings(roots["ordinary"], directory, after)
                return
            key = self._seek_lexical(roots, directory, after, strict=after is not None)
            while key is not None:
                yield self._joined(roots, directory, key, required=True)
                key = self._seek_lexical(roots, directory, key, strict=True)

    def search_field(self, root, prefix, query, *, after=None, origin_commit_at_most=None):
        normalized = query.casefold()
        if not normalized:
            raise ValueError("empty owner content pages: query")
        if origin_commit_at_most is not None and not index_module._integer(origin_commit_at_most):
            raise ValueError("origin cut must be a nonnegative integer")
        width = min(3, len(normalized))
        grams = {}
        for offset in range(len(normalized) - width + 1):
            grams.setdefault(normalized[offset:offset + width], []).append(offset)
        directories = [tuple(prefix) + (gram,) for gram in grams]
        for directory in directories:
            index_module._validate_directory_key(directory)
        with self._operation():
            roots = self._roots(root)
            keys = [self._seek_lexical(roots, directory, after, strict=after is not None) for directory in directories]
            while all(key is not None for key in keys):
                largest = max(keys)
                if any(key != largest for key in keys):
                    keys = [self._seek_lexical(roots, directory, largest) if key < largest else key
                            for directory, key in zip(directories, keys)]
                    continue
                prepared = self._resolved_occurrence(roots, directories[0], largest, required=True)
                postings = [self._joined_occurrence(prepared, directory, largest, required=True) for directory in directories]
                first = postings[0]
                starts = None
                for posting, offsets in zip(postings, grams.values()):
                    if any(getattr(posting, name) != getattr(first, name) for name in (
                            *_SOURCE_FIELDS, "field_length")):
                        raise self.integrity_error("owner content pages: intersected source qualifications differ")
                    for offset in offsets:
                        shifted = {position - offset for position in posting.positions if position >= offset}
                        starts = shifted if starts is None else starts & shifted
                if starts and (origin_commit_at_most is None or first.origin_commit <= origin_commit_at_most):
                    yield first
                # Strict progress after both accepted and rejected alignments.
                keys[0] = self._seek_lexical(roots, directories[0], largest, strict=True)

    def _audit_actual_row(self, scratch, directory, posting):
        # Reuse the existing sorter table/order, NOT its expected-source
        # equal-key deduplication. Repeated actual membership is an error even
        # when both values happen to be equal.
        try:
            scratch.execute("INSERT INTO entries VALUES (?,?,?,?,?)", (
                index_module._tuple_sort_key(directory), index_module._tuple_sort_key(posting.key),
                canonical_json_bytes(directory).decode("utf-8"), canonical_json_bytes(posting.key).decode("utf-8"),
                posting._canonical_bytes().decode("utf-8")))
        except sqlite3.IntegrityError as exc:
            raise self.integrity_error("owner content pages: duplicate actual audit membership") from exc

    def _audit_occurrences(self, root, scratch):
        """ONE complete structural traversal for both audit representations.

        Consumers validate each yielded occurrence's lexical meaning. Neither
        public adapter returns success before exhausting the inverse checks.
        Ordinary rows remain in scratch; lexical Postings are not required here.
        """
        scratch.execute("CREATE TABLE occurrence_audit (role TEXT, key_sort BLOB, key_json TEXT, value_json TEXT, "
                        "PRIMARY KEY(role,key_sort)) WITHOUT ROWID")
        expected_counts = {}
        inverse_completed = 0
        inverse_total = None

        def expected_row(role, key, value):
            try:
                scratch.execute("INSERT INTO occurrence_audit VALUES (?,?,?,?)", (role,
                    index_module._tuple_sort_key(key), canonical_json_bytes(key).decode("utf-8"),
                    canonical_json_bytes(value).decode("utf-8")))
                expected_counts[role] = expected_counts.get(role, 0) + 1
            except sqlite3.IntegrityError as exc:
                raise self.integrity_error("owner content pages: duplicate audit " + role) from exc

        def expected_rows(role):
            with closing(scratch.execute("SELECT key_json,value_json FROM occurrence_audit WHERE role=? ORDER BY key_sort",
                                         (role,))) as cursor:
                for key, value in cursor:
                    yield key.encode("utf-8"), value.encode("utf-8")

        def compare(role, actual):
            nonlocal inverse_completed
            with closing(expected_rows(role)) as wanted, closing(actual) as observed:
                for left, right in zip_longest(wanted, observed):
                    if left != right:
                        raise self.integrity_error("owner content pages: " + role + " inverse/field-map relation differs")
                    inverse_completed += 1
                    if inverse_completed % 256 == 0:
                        report_preparation_progress("index_audit_inverses", inverse_completed, inverse_total)

        roots = self._roots(root)
        ordinary_completed = 0
        report_preparation_progress("index_audit_inverses")
        with closing(super().iter_entries(roots["ordinary"])) as ordinary:
            for directory, posting in ordinary:
                if directory[0] not in {"owner_inventory", "current_field_presence", "capture_epoch_kind"}:
                    # Otherwise a rehashed lexical fact moved out of its
                    # map/inverse could preserve source equality but hide
                    # from queries. Ordinary storage is not an escape path.
                    raise self.integrity_error("owner content pages: ordinary root has a lexical membership")
                self._audit_actual_row(scratch, directory, posting)
                if directory[0] == "owner_inventory":
                    expected_row("observed_inventory", self._owner(directory, posting.key) + (posting.key[2], directory[3]), {})
                ordinary_completed += 1
                if ordinary_completed % 256 == 0:
                    report_preparation_progress("index_audit_inverses", ordinary_completed)
        report_preparation_progress("index_audit_inverses", ordinary_completed, ordinary_completed)
        last_owner, prior_presence = None, set()
        occurrence_completed = 0
        occurrence_total = roots["occurrences"]["count"]
        report_preparation_progress("index_audit_occurrences", 0, occurrence_total)
        with closing(self._items(roots["occurrences"], self._scope("occurrences"))) as occurrences:
            for identity, binding in occurrences:
                prepared = self._prepared_occurrence(identity, binding)
                owner = identity[:-1]
                if owner != last_owner:
                    last_owner, prior_presence = owner, set()
                facts, selections = prepared.facts, []
                for selected, flag in (("current_at_cut", "current"), ("retained_history", "retained")):
                    if binding[flag]:
                        expected_row("inventory", identity + (selected,), {})
                        # Ordinary traversal above already authenticated every
                        # exact typed row and rejected duplicates. Reuse only
                        # that audit-local row, not another tree point read or
                        # a writer/expected-source value. Public/update reads
                        # still use _inventory_at and authenticate their roots.
                        directory = ("owner_inventory", owner[0], owner[1], selected, owner[2], owner[3])
                        key = (owner[3], owner[4], identity[-1])
                        inventory = scratch.execute("SELECT value_json FROM entries WHERE directory_sort=? AND posting_sort=?",
                            (index_module._tuple_sort_key(directory), index_module._tuple_sort_key(key))).fetchone()
                        if inventory is None:
                            raise self.integrity_error("owner content pages: occurrence inventory is absent")
                        value = index_module.loads_strict_json_object(inventory[0])
                        if not _same_json(
                                self._source_value(value, inventory=True), prepared.source):
                            raise self.integrity_error("owner content pages: source group mixes qualifications")
                        del value, inventory
                        selections.append(selected)
                yield prepared, tuple(selections)
                if binding["current"]:
                    expected_row("heads", owner, {"revision": identity[-1]})
                    for field, gram in facts:
                        expected_row("current", owner[:4] + (field, gram, owner[4]), {"present": True})
                if binding["retained"]:
                    for field, gram in prior_presence ^ facts.keys():
                        expected_row("events", owner[:4] + (field, gram, owner[4], identity[-1]),
                                     {"present": (field, gram) in facts})
                    prior_presence = set(facts)
                # No previous map's bodies survive into the next source
                # outside the existing bounded operation cache.
                del prepared, facts
                occurrence_completed += 1
                report_preparation_progress("index_audit_occurrences", occurrence_completed, occurrence_total)
        inverse_total = sum(expected_counts.get(name, 0) for name in ("inventory", "heads", "current", "events"))
        report_preparation_progress("index_audit_inverses", 0, inverse_total)
        compare("inventory", expected_rows("observed_inventory"))
        for name in ("heads", "current", "events"):
            with closing(self._items(roots[name], self._scope(name))) as actual:
                # Canonical bytes distinguish 1/true in keys AND values;
                # dictionary/Python equality cannot provide that check.
                compare(name, ((canonical_json_bytes(key), canonical_json_bytes(value)) for key, value in actual))
        report_preparation_progress("index_audit_inverses", inverse_completed, inverse_total)

    def iter_entries(self, root):
        # Preserve the complete logical iterator for its existing consumers.
        # All internal relations are checked before the first outward row.
        with self._operation(), index_module._SortedEntries((), integrity_error=self.integrity_error) as ordered:
            scratch = ordered._database()
            with closing(self._audit_occurrences(root, scratch)) as occurrences:
                for prepared, selections in occurrences:
                    for directory, posting in self._logical_scopes(prepared, selections):
                        self._audit_actual_row(scratch, directory, posting)
                    del prepared
            with closing(ordered.iter_entries()) as actual:
                yield from actual

    def audit_sources(self, root, inventory, components=None):
        """Compare fresh SOURCE meaning, without expanding lexical Postings.

        Scratch holds qualified source text/descriptors, not an all-history
        Python inventory. One actual map, one expected field's grams and the
        existing bounded LRU remain materialized. The caller authenticates
        source custody; this comparison does not replace that authority.
        """
        from research_core.owner_content_access import source_descriptor_entries

        started = time.perf_counter()
        source_visits = ordinary_total = 0
        report_preparation_progress("index_audit_sources")
        with self._operation(), index_module._SortedEntries((), integrity_error=self.integrity_error) as ordered:
            scratch = ordered._database()
            scratch.execute("CREATE TABLE source_audit_expected (identity_sort BLOB PRIMARY KEY, source_json TEXT, "
                            "fields_json TEXT, inventory_json TEXT, is_current INTEGER, is_retained INTEGER, seen INTEGER) WITHOUT ROWID")
            scratch.execute("CREATE TABLE source_audit_ordinary (directory_sort BLOB, posting_sort BLOB, value_json TEXT, "
                            "PRIMARY KEY(directory_sort,posting_sort)) WITHOUT ROWID")
            for selected, source in inventory:
                for directory, posting in source_descriptor_entries(source, selected):
                    key = (index_module._tuple_sort_key(directory), index_module._tuple_sort_key(posting.key))
                    encoded = posting._canonical_bytes().decode("utf-8")
                    previous = scratch.execute("SELECT value_json FROM source_audit_ordinary "
                                               "WHERE directory_sort=? AND posting_sort=?", key).fetchone()
                    if previous is None:
                        scratch.execute("INSERT INTO source_audit_ordinary VALUES (?,?,?)", (*key, encoded))
                        ordinary_total += 1
                    elif previous[0] != encoded:
                        raise self.integrity_error("owner content pages: source audit has conflicting descriptor membership")
                    if directory[0] != "owner_inventory":
                        continue  # Presence is once per source, NOT per variant.
                    identity = self._owner(directory, posting.key) + (posting.key[2],)
                    identity_sort = index_module._tuple_sort_key(identity)
                    source_json = canonical_json_bytes(self._source_value(posting.to_mapping(), inventory=True)).decode("utf-8")
                    # Casefolded text is a lossless normalization for this
                    # lexical relation; raw case differences were never keys.
                    fields_json = canonical_json_bytes({name: text.casefold()
                        for name, text in source.fields[directory[4]].items()}).decode("utf-8")
                    flags = (int(selected == "current_at_cut"), int(selected == "retained_history"))
                    previous = scratch.execute("SELECT source_json,fields_json,inventory_json FROM source_audit_expected WHERE identity_sort=?",
                                               (identity_sort,)).fetchone()
                    if previous is None:
                        scratch.execute("INSERT INTO source_audit_expected VALUES (?,?,?,?,?,?,0)",
                                        (identity_sort, source_json, fields_json, encoded, *flags))
                    elif previous != (source_json, fields_json, encoded):
                        raise self.integrity_error("owner content pages: source audit occurrence qualifications differ")
                    else:
                        scratch.execute("UPDATE source_audit_expected SET is_current=MAX(is_current,?),is_retained=MAX(is_retained,?) "
                                        "WHERE identity_sort=?", (*flags, identity_sort))
                source_visits += 1
                report_preparation_progress("index_audit_sources", source_visits)
            report_preparation_progress("index_audit_sources", source_visits, source_visits)
            prepared_at = time.perf_counter()
            count = 0
            with closing(self._audit_occurrences(root, scratch)) as occurrences:
                for prepared, selections in occurrences:
                    identity, binding = prepared.identity, prepared.value
                    identity_sort = index_module._tuple_sort_key(identity)
                    expected = scratch.execute("SELECT source_json,fields_json,is_current,is_retained,seen "
                        "FROM source_audit_expected WHERE identity_sort=?", (identity_sort,)).fetchone()
                    if expected is None or expected[4]:
                        raise self.integrity_error("owner content pages: source audit has an extra or repeated occurrence")
                    source_json, fields_json, current, retained, _seen = expected
                    if (canonical_json_bytes(prepared.source).decode("utf-8") != source_json
                            or (int(binding["current"]), int(binding["retained"])) != (current, retained)):
                        raise self.integrity_error("owner content pages: source audit occurrence qualifications differ")
                    fields = index_module.loads_strict_json_object(fields_json)
                    lexical_count = self._assert_source_fields(prepared, fields, selections[0])
                    count += lexical_count * len(selections)
                    scratch.execute("UPDATE source_audit_expected SET seen=1 WHERE identity_sort=?", (identity_sort,))
                    del prepared, fields
            if scratch.execute("SELECT 1 FROM source_audit_expected WHERE seen=0 LIMIT 1").fetchone() is not None:
                raise self.integrity_error("owner content pages: source audit omitted an eligible occurrence")
            ordinary_completed = 0
            report_preparation_progress("index_audit_inverses", 0, ordinary_total)
            with closing(scratch.execute("SELECT directory_sort,posting_sort,value_json FROM source_audit_ordinary "
                                         "ORDER BY directory_sort,posting_sort")) as expected:
                with closing(scratch.execute("SELECT directory_sort,posting_sort,value_json FROM entries "
                                             "ORDER BY directory_sort,posting_sort")) as actual:
                    for wanted, observed in zip_longest(expected, actual):
                        if wanted != observed:
                            raise self.integrity_error("owner content pages: source audit ordinary relation differs")
                        count += 1
                        ordinary_completed += 1
                        if ordinary_completed % 256 == 0:
                            report_preparation_progress("index_audit_inverses", ordinary_completed, ordinary_total)
            report_preparation_progress("index_audit_inverses", ordinary_completed, ordinary_total)
            compared_at = time.perf_counter()
        if components is not None:
            components.update(source_derivation_and_sort_seconds=prepared_at - started,
                ordered_source_graph_comparison_seconds=compared_at - prepared_at,
                scratch_cleanup_seconds=time.perf_counter() - compared_at)
        return count
