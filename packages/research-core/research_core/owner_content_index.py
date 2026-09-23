"""Store-private, authenticated positional access to authorized owner projections.

The Store supplies the durable connection, schema, sealed descriptor and writer
transaction. This module owns no authorization or source projection. Explicit
bootstrap/audit may use a separately connected, auto-deleted sort scratch file;
that scratch never attaches to or changes the protected Store schema.
Reads follow authenticated compact range pages; SQL absence never proves a miss.
Keys use Python tuple order with numeric revision ordinals (not JSON text order).
The manifest and each logical relation have distinct authenticated scopes. Complete
source-to-index rederivation remains the Store audit owner's responsibility.
"""

from __future__ import annotations

import hashlib
import math
import re
import sqlite3
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable, Iterator, Mapping, Sequence

from .json_support import canonical_json_bytes, loads_strict_json, loads_strict_json_object
from .owner_content_projection import (
    CHECKPOINT_PRESENCE_FIELDS,
    PROJECTION_FIELDS,
    SEARCH_FIELDS,
)


DirectoryKey = tuple[str, ...]
PostingKey = tuple[str, str, int] | tuple[str, str]
_VARIANT_KINDS = {
    variant: frozenset(kinds) for variant, kinds in PROJECTION_FIELDS.items()
}
_PRESENCE_FIELDS = CHECKPOINT_PRESENCE_FIELDS
_DOMAIN = "mathematical_research.owner_content_index.v2"
_DIGEST = re.compile(r"[0-9a-f]{64}\Z")


def _hash(value: Any) -> str:
    return hashlib.sha256(canonical_json_bytes(value)).hexdigest()


def _digest(value: Any) -> bool:
    return isinstance(value, str) and _DIGEST.fullmatch(value) is not None


def _integer(value: Any, minimum: int = 0) -> bool:
    return type(value) is int and value >= minimum


def _json_metadata(value: Any) -> bool:
    if isinstance(value, Mapping):
        return all(isinstance(key, str) and _json_metadata(item) for key, item in value.items())
    if isinstance(value, (list, tuple)):
        return all(_json_metadata(item) for item in value)
    return (value is None or type(value) in (str, int, bool)
            or (type(value) is float and math.isfinite(value)))


def lexical_directory_key(
    project_id: str, mission_id: str, revision_scope: str, variant: str,
    kind: str, field: str, gram: str,
) -> DirectoryKey:
    key = ("lexical", project_id, mission_id, revision_scope, variant, kind, field, gram)
    _validate_directory_key(key)
    return key


def presence_directory_key(
    project_id: str, mission_id: str, kind: str, field: str,
) -> DirectoryKey:
    key = ("current_field_presence", project_id, mission_id, kind, field)
    _validate_directory_key(key)
    return key


def inventory_directory_key(
    project_id: str, mission_id: str, revision_scope: str, variant: str, kind: str,
) -> DirectoryKey:
    key = ("owner_inventory", project_id, mission_id, revision_scope, variant, kind)
    _validate_directory_key(key)
    return key


def capture_epoch_kind_directory_key(
    project_id: str, mission_id: str, origin_epoch_id: str | None, capture_kind: str,
) -> DirectoryKey:
    key = ("capture_epoch_kind", project_id, mission_id, "current_at_cut", "root", "capture",
           "none" if origin_epoch_id is None else "epoch",
           "" if origin_epoch_id is None else origin_epoch_id, capture_kind)
    _validate_directory_key(key)
    return key


def _validate_directory_key(key: DirectoryKey) -> None:
    if not isinstance(key, tuple) or any(not isinstance(x, str) for x in key):
        raise ValueError("owner content directory key must be a string tuple")
    if key and key[0] == "lexical":
        if (
            len(key) != 8 or not key[1] or not key[2]
            or key[3] not in ("retained_history", "current_at_cut")
            or key[5] not in _VARIANT_KINDS.get(key[4], ())
            or key[6] not in SEARCH_FIELDS
            or not 1 <= len(key[7]) <= 3 or key[7] != key[7].casefold()
        ):
            raise ValueError("owner content lexical key is outside its closed profile")
    elif key and key[0] == "owner_inventory":
        if (
            len(key) != 6 or not key[1] or not key[2]
            or key[3] not in ("retained_history", "current_at_cut")
            or key[5] not in _VARIANT_KINDS.get(key[4], ())
        ):
            raise ValueError("owner content inventory key is outside its closed profile")
    elif key and key[0] == "current_field_presence":
        if (len(key) != 5 or not key[1] or not key[2]
                or key[4] not in _PRESENCE_FIELDS.get(key[3], ())):
            raise ValueError("owner content presence key is outside its closed profile")
    elif key and key[0] == "capture_epoch_kind":
        if (len(key) != 9 or not key[1] or not key[2]
                or key[3:6] != ("current_at_cut", "root", "capture")
                or not (key[6:8] == ("none", "") or (key[6] == "epoch" and bool(key[7])))
                or key[8] not in ("assignment", "output")):
            raise ValueError("owner content Capture epoch/kind key is outside its closed profile")
    else:
        raise ValueError("owner content directory namespace is unknown")


@dataclass(frozen=True)
class Posting:
    key: PostingKey
    reference: str
    payload_digest: str
    origin_commit: int
    positions: tuple[int, ...] = ()
    field_length: int | None = None
    source_origin_commit: int | None = None
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def _canonical_bytes(self) -> bytes:
        """Encode the exact posting value without retaining caller-owned data."""
        if not isinstance(self.metadata, Mapping) or not _json_metadata(self.metadata):
            raise ValueError("owner content posting metadata must be finite string-keyed JSON")
        value = {
            "reference": self.reference, "payload_digest": self.payload_digest,
            "origin_commit": self.origin_commit,
            "source_origin_commit": (
                self.origin_commit if self.source_origin_commit is None
                else self.source_origin_commit
            ),
            "positions": list(self.positions), "field_length": self.field_length,
            "metadata": dict(self.metadata),
        }
        return canonical_json_bytes(value)

    def to_mapping(self) -> dict[str, Any]:
        return dict(loads_strict_json_object(self._canonical_bytes()))


def _validate_posting_value(key: tuple[Any, ...], value: Mapping[str, Any], scope: DirectoryKey) -> None:
    """Validate a posting without allocating a discarded outward copy."""
    if set(value) != {
        "reference", "payload_digest", "origin_commit", "source_origin_commit",
        "positions", "field_length", "metadata",
    }:
        raise ValueError("owner content posting value has an unknown shape")
    lexical = scope[0] == "lexical"
    revisioned = scope[0] != "current_field_presence"
    kind = scope[5] if revisioned else scope[3]
    positions = value["positions"]
    if (
        len(key) != (3 if revisioned else 2) or key[0] != kind
        or not isinstance(key[1], str) or not key[1]
        or (revisioned and not _integer(key[2]))
        or not isinstance(value["reference"], str) or not value["reference"]
        or not _digest(value["payload_digest"])
        or not _integer(value["origin_commit"])
        or not _integer(value["source_origin_commit"])
        or value["source_origin_commit"] > value["origin_commit"]
        or not isinstance(value["metadata"], Mapping) or not _json_metadata(value["metadata"])
        or not isinstance(positions, list)
        or any(not _integer(position) for position in positions)
        or any(a >= b for a, b in zip(positions, positions[1:]))
    ):
        raise ValueError("owner content posting identity, origin or positions are invalid")
    if lexical:
        if (not positions or not _integer(value["field_length"], len(scope[7]))
                or positions[-1] + len(scope[7]) > value["field_length"]):
            raise ValueError("owner content positions exceed the casefolded field")
    elif positions or value["field_length"] is not None:
        raise ValueError("nonlexical postings cannot contain lexical positions")


def _posting_from_value(key: tuple[Any, ...], value: Mapping[str, Any], scope: DirectoryKey) -> Posting:
    _validate_posting_value(key, value, scope)
    return Posting(
        key=key, reference=value["reference"], payload_digest=value["payload_digest"],
        origin_commit=value["origin_commit"], positions=tuple(value["positions"]),
        field_length=value["field_length"], source_origin_commit=value["source_origin_commit"],
        metadata=loads_strict_json_object(canonical_json_bytes(value["metadata"])),
    )


def _tuple_sort_key(key: tuple[Any, ...]) -> bytes:
    """Injective byte order for the existing string/nonnegative-integer tuples."""
    components = []
    for part in key:
        if isinstance(part, str):
            # UTF-8 preserves scalar codepoint order. Escaped NUL and a smaller
            # terminator retain Python's string-prefix and tuple-prefix order.
            components.append(b"s" + part.encode("utf-8").replace(b"\0", b"\0\xff") + b"\0\0")
        elif _integer(part):
            digits = str(part).encode("ascii")
            # Do not narrow the posting codec's integers to SQLite int64.
            components.append(b"i" + b"\1" * len(digits) + b"\0" + digits)
        else:
            raise ValueError("owner content sort key has an unsupported component")
    return b"".join(components)


class _SortedEntries:
    """One explicit bootstrap/audit's private disk-backed, bounded-cache sort.

    Values are validated before insertion. Exact-key duplicates retain the
    first value under the existing mapping-equality rule; conflicting values
    fail. Directory and posting keys are distinct BLOB columns, never JSON
    textual ordering or a narrowed SQLite numeric ordinal. The context owns a
    private temporary directory and fixed ordinary disk database, closing the
    connection before removing the directory on success or failure.
    """

    def __init__(
        self, entries: Iterable[tuple[DirectoryKey, Posting]],
        *, integrity_error: Callable[[str], Exception] = ValueError,
    ) -> None:
        self._entries = entries
        self._integrity_error = integrity_error
        self._connection: sqlite3.Connection | None = None
        self._directory: tempfile.TemporaryDirectory | None = None

    def __enter__(self) -> _SortedEntries:
        if self._connection is not None or self._directory is not None:
            raise ValueError("owner content scratch is already open")
        self._directory = tempfile.TemporaryDirectory(prefix="rh-owner-content-")
        try:
            # An anonymous main DB can become memory-backed at open time under
            # SQLite compile defaults, before a later temp_store pragma applies.
            connection = sqlite3.connect(str(Path(self._directory.name) / "main.sqlite3"))
            self._connection = connection
            # Requested page-cache budget, not a whole-process memory ceiling.
            connection.execute("PRAGMA cache_size = -2048")
            connection.execute("PRAGMA temp_store = FILE")
            connection.execute("CREATE TABLE entries ("
                "directory_sort BLOB NOT NULL, posting_sort BLOB NOT NULL, "
                "directory_json TEXT NOT NULL, key_json TEXT NOT NULL, value_json TEXT NOT NULL, "
                "PRIMARY KEY (directory_sort, posting_sort)) WITHOUT ROWID")
            for directory, posting in self._entries:
                _validate_directory_key(directory)
                encoded_value = posting._canonical_bytes()
                value = loads_strict_json_object(encoded_value)
                _validate_posting_value(posting.key, value, directory)
                directory_sort, posting_sort = _tuple_sort_key(directory), _tuple_sort_key(posting.key)
                inserted = connection.execute(
                    "INSERT INTO entries VALUES (?,?,?,?,?) ON CONFLICT DO NOTHING",
                    (directory_sort, posting_sort, canonical_json_bytes(directory).decode("utf-8"),
                     canonical_json_bytes(posting.key).decode("utf-8"), encoded_value.decode("utf-8")),
                ).rowcount
                if not inserted:
                    previous = connection.execute(
                        "SELECT value_json FROM entries WHERE directory_sort=? AND posting_sort=?",
                        (directory_sort, posting_sort),
                    ).fetchone()
                    if previous is None or loads_strict_json_object(previous[0]) != value:
                        raise self._integrity_error("owner content index: source rederivation has conflicting membership")
            connection.commit()
            self._entries = ()
            return self
        except BaseException:
            self.__exit__()
            raise

    def __exit__(self, *_exception: Any) -> None:
        try:
            if self._connection is not None:
                self._connection.close()
        finally:
            self._connection = None
            if self._directory is not None:
                self._directory.cleanup()
                self._directory = None

    def _database(self) -> sqlite3.Connection:
        if self._connection is None:
            raise ValueError("owner content scratch is not open")
        return self._connection

    @property
    def directory_count(self) -> int:
        return int(self._database().execute(
            "SELECT COUNT(*) FROM (SELECT directory_sort FROM entries GROUP BY directory_sort)"
        ).fetchone()[0])

    def groups(self) -> Iterator[tuple[DirectoryKey, int]]:
        for directory, count in self._database().execute(
            "SELECT directory_json, COUNT(*) FROM entries "
            "GROUP BY directory_sort ORDER BY directory_sort"
        ):
            yield tuple(loads_strict_json(directory)), int(count)

    def _values(self, directory: DirectoryKey) -> Iterator[tuple[PostingKey, Mapping[str, Any]]]:
        for key, value in self._database().execute(
            "SELECT key_json, value_json FROM entries WHERE directory_sort=? ORDER BY posting_sort",
            (_tuple_sort_key(directory),),
        ):
            yield tuple(loads_strict_json(key)), loads_strict_json_object(value)

    def iter_entries(self) -> Iterator[tuple[DirectoryKey, Posting]]:
        for directory, key, value in self._database().execute(
            "SELECT directory_json, key_json, value_json FROM entries ORDER BY directory_sort, posting_sort"
        ):
            scope = tuple(loads_strict_json(directory))
            yield scope, _posting_from_value(tuple(loads_strict_json(key)), loads_strict_json_object(value), scope)


def entries_for_inventory(
    *, project_id: str, mission_id: str, revision_scope: str, variant: str,
    kind: str, owner_id: str, revision: int, reference: str, payload_digest: str,
    origin_commit: int, source_origin_commit: int | None = None,
    metadata: Mapping[str, Any] | None = None,
) -> Iterator[tuple[DirectoryKey, Posting]]:
    """Descriptor-only keyed inventory; no owner-text or capture-body scan."""
    directory = inventory_directory_key(project_id, mission_id, revision_scope, variant, kind)
    posting = Posting(
        (kind, owner_id, revision), reference, payload_digest, origin_commit,
        source_origin_commit=source_origin_commit, metadata=metadata or {},
    )
    _validate_posting_value(posting.key, posting.to_mapping(), directory)
    yield directory, posting


def _field_gram_positions(fields: Mapping[str, str]) -> Iterator[tuple[str, int, dict[str, list[int]]]]:
    """The single lexical derivation, before physical Posting/source factoring.

    One field's complete gram map is materialized, as in the Posting wrapper;
    this is not a constant-memory claim for arbitrarily large fields.
    """
    for name in sorted(fields):
        if not isinstance(fields[name], str):
            raise ValueError("owner search projections must supply strings")
        content = fields[name].casefold()
        grams: dict[str, list[int]] = {}
        for width in (1, 2, 3):
            for position in range(len(content) - width + 1):
                grams.setdefault(content[position:position + width], []).append(position)
        yield name, len(content), grams


def entries_for_fields(
    *, project_id: str, mission_id: str, revision_scope: str, variant: str,
    kind: str, owner_id: str, revision: int, reference: str, payload_digest: str,
    origin_commit: int, fields: Mapping[str, str], source_origin_commit: int | None = None,
    metadata: Mapping[str, Any] | None = None,
) -> Iterator[tuple[DirectoryKey, Posting]]:
    """Derive only supplied authorized fields, with exact casefold substring rules."""
    for name, field_length, grams in _field_gram_positions(fields):
        for gram in sorted(grams):
            directory = lexical_directory_key(
                project_id, mission_id, revision_scope, variant, kind, name, gram,
            )
            posting = Posting(
                (kind, owner_id, revision), reference, payload_digest, origin_commit,
                tuple(grams[gram]), field_length, source_origin_commit, metadata or {},
            )
            _validate_posting_value(posting.key, posting.to_mapping(), directory)
            yield directory, posting
        # Release the preceding field before the derivation generator builds
        # the next map; extraction must not retain two full field maps here.
        del grams


def entries_for_presence(
    *, project_id: str, mission_id: str, kind: str, owner_id: str,
    reference: str, payload_digest: str, origin_commit: int,
    document: Mapping[str, Any], source_origin_commit: int | None = None,
    metadata: Mapping[str, Any] | None = None,
) -> Iterator[tuple[DirectoryKey, Posting]]:
    """Preserve precisely the five existing bool(document.get(field, ())) predicates."""
    for name in _PRESENCE_FIELDS.get(kind, ()):
        if bool(document.get(name, ())):
            directory = presence_directory_key(project_id, mission_id, kind, name)
            posting = Posting(
                (kind, owner_id), reference, payload_digest, origin_commit,
                source_origin_commit=source_origin_commit, metadata=metadata or {},
            )
            _validate_posting_value(posting.key, posting.to_mapping(), directory)
            yield directory, posting


def _page_engine(connection, profile_digest, integrity_error):
    # Lazy import: the physical codec shares this module's logical primitives.
    from .owner_content_pages import OccurrenceIndex, PageObjectStore
    store = PageObjectStore(connection, profile_sha256=profile_digest,
        integrity_error=integrity_error, table="owner_content_index_node",
        digest_column="node_digest", kind_column="node_kind", body_column="node_json",
        domain=_DOMAIN)
    return OccurrenceIndex(store, page_bytes=4096, compact_page_keys=True,
                           cache_items_limit=1024, cache_bytes_limit=8 * 1024 * 1024)


def validate_descriptor(value: Mapping[str, Any], profile_digest: str) -> dict[str, Any]:
    """Validate the closed v2 envelope; graph authentication remains a read."""
    if (not _digest(profile_digest) or not isinstance(value, Mapping)
            or set(value) != {"contract_version", "projection_profile_digest", "root"}
            or type(value["contract_version"]) is not int or value["contract_version"] != 2
            or value["projection_profile_digest"] != profile_digest):
        raise ValueError("owner content descriptor/profile is outside version 2")
    root = value["root"]
    if (not isinstance(root, Mapping)
            or set(root) != {"digest", "kind", "count", "first", "last", "height", "used_bytes"}
            or not _digest(root["digest"]) or root["kind"] not in {"range-cell", "range-branch"}
            or type(root["count"]) is not int or root["count"] != 5
            or not isinstance(root["first"], (list, tuple)) or not isinstance(root["last"], (list, tuple))
            or list(root["first"]) != ["current"] or list(root["last"]) != ["ordinary"]
            or not _integer(root["height"]) or not _integer(root["used_bytes"])
            or root["used_bytes"] > 4096
            or (root["kind"] == "range-cell") != (root["height"] == 0)):
        raise ValueError("owner content descriptor has an invalid manifest summary")
    return dict(loads_strict_json_object(canonical_json_bytes(value)))


def empty_descriptor(profile_digest: str) -> dict[str, Any]:
    """Pure exact genesis binding, generated by the ordinary page codec."""
    engine = _page_engine(None, profile_digest, ValueError)
    return {"contract_version": 2, "projection_profile_digest": profile_digest,
            "root": engine.empty_root()}


class OwnerContentIndex:
    """One Store operation's authenticated compact-index view.

    The Store supplies schema, source authorization, sealed descriptor and the
    only writer transaction. Bootstrap and deltas persist through the same
    page engine; this facade never creates a table or commits a transaction.
    """

    def __init__(
        self, connection: sqlite3.Connection, profile_digest: str,
        descriptor: Mapping[str, Any] | None = None,
        *, integrity_error: Callable[[str], Exception] = ValueError,
    ) -> None:
        self.connection = connection
        self.profile_digest, self.integrity_error = profile_digest, integrity_error
        self._engine = _page_engine(connection, profile_digest, integrity_error)
        self._root = None
        if descriptor is not None:
            try:
                self._root = validate_descriptor(descriptor, profile_digest)["root"]
            except (TypeError, ValueError) as exc:
                raise integrity_error(str(exc)) from exc
            # A sealed but absent/malformed manifest is never an empty index.
            with self._engine.operation():
                self._engine._roots(self._root)

    def _required_root(self):
        if self._root is None:
            raise self.integrity_error("owner content index has not been bootstrapped")
        return self._root

    def _require_writer(self):
        if not self.connection.in_transaction:
            raise self.integrity_error("owner content maintenance requires the Store writer transaction")

    @property
    def descriptor(self) -> dict[str, Any]:
        return validate_descriptor({"contract_version": 2,
            "projection_profile_digest": self.profile_digest, "root": self._required_root()},
            self.profile_digest)

    @property
    def node_reads(self) -> int:
        return self._engine.packed.node_reads

    @property
    def nodes_written(self) -> int:
        return self._engine.packed.nodes_written

    def operation(self):
        """Bound reuse to one explicitly owned request, never across requests."""
        return self._engine.operation()

    def bootstrap(self, inventory) -> dict[str, Any]:
        self._require_writer()
        if self._root is not None:
            raise self.integrity_error("owner content bootstrap requires an uninitialized index")
        if self.connection.execute("SELECT 1 FROM owner_content_index_node LIMIT 1").fetchone() is not None:
            raise self.integrity_error("owner content bootstrap requires empty index storage")
        self._root = self._engine.build(inventory)
        return self.descriptor

    def apply_delta(self, delta) -> dict[str, Any]:
        self._require_writer()
        self._root = self._engine.replace_source(self._required_root(), delta)
        return self.descriptor

    def audit_sources(self, inventory, components=None) -> int:
        """Full independent source/graph audit, without a duplicate first traversal."""
        return self._engine.audit_sources(self._required_root(), inventory, components)

    def postings(self, directory: DirectoryKey, *, after=None) -> Iterator[Posting]:
        yield from self._engine.postings(self._required_root(), directory, after)

    def posting(self, directory: DirectoryKey, key: PostingKey) -> Posting | None:
        return self._engine.posting(self._required_root(), directory, key)

    def search_field(
        self, *, directory_prefix: DirectoryKey, query: str,
        after=None, origin_commit_at_most: int | None = None,
    ) -> Iterator[Posting]:
        yield from self._engine.search_field(self._required_root(), directory_prefix, query,
            after=after, origin_commit_at_most=origin_commit_at_most)

    @staticmethod
    def page(values: Iterable[Posting], limit: int) -> tuple[tuple[Posting, ...], bool]:
        if not _integer(limit, 1):
            raise ValueError("page limit must be a positive integer")
        iterator = iter(values)
        try:
            result: list[Posting] = []
            for _ in range(limit):
                value = next(iterator, None)
                if value is None:
                    return tuple(result), False
                result.append(value)
            return tuple(result), next(iterator, None) is not None
        finally:
            close = getattr(iterator, "close", None)
            if close is not None:
                close()

    def iter_entries(self) -> Iterator[tuple[DirectoryKey, Posting]]:
        """Explicit complete logical audit traversal, never a query fallback."""
        yield from self._engine.iter_entries(self._required_root())
