"""Strict JSON primitives for Mathematical Research authority boundaries.

The standard :mod:`json` decoder otherwise accepts duplicate object keys and
the non-standard ``NaN``/``Infinity`` constants.  Authority-bearing inputs use
this module so that every accepted byte sequence has one finite JSON meaning.
Remote-execution contracts keep their existing contract-specific parsers until
they are deliberately migrated; importing this module does not change them.
"""

from __future__ import annotations

import json
from dataclasses import asdict, is_dataclass
from enum import Enum
from typing import Any, Mapping, Sequence


class DuplicateKeyError(ValueError):
    """Raised when JSON would otherwise silently overwrite an object key."""


class NonFiniteJSONError(ValueError):
    """Raised when JSON contains a non-standard non-finite numeric constant."""


def unique_json_object(pairs: Sequence[tuple[str, Any]]) -> dict[str, Any]:
    """Build one object while rejecting duplicate keys at every depth."""

    value: dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            raise DuplicateKeyError(f"duplicate JSON key: {key}")
        value[key] = item
    return value


def reject_nonfinite_constant(value: str) -> None:
    """Reject ``NaN`` and infinities accepted as extensions by ``json``."""

    raise NonFiniteJSONError(f"non-finite JSON constant: {value}")


def loads_strict_json(text: str) -> Any:
    """Decode finite, duplicate-key-safe JSON text."""

    return json.loads(
        text,
        object_pairs_hook=unique_json_object,
        parse_constant=reject_nonfinite_constant,
    )


def loads_strict_json_bytes(raw: bytes) -> Any:
    """Decode strict UTF-8 JSON bytes without accepting replacement text."""

    return loads_strict_json(raw.decode("utf-8", errors="strict"))


def loads_strict_json_object(raw: bytes | str) -> Mapping[str, Any]:
    """Decode a strict JSON document whose top level must be an object."""

    value = (
        loads_strict_json_bytes(raw)
        if isinstance(raw, bytes)
        else loads_strict_json(raw)
    )
    if not isinstance(value, Mapping):
        raise TypeError("top-level JSON must be an object")
    return value


def json_compatible(value: Any) -> Any:
    """Normalize immutable/domain containers into deterministic JSON values."""

    if isinstance(value, Enum):
        return json_compatible(value.value)
    if isinstance(value, Mapping):
        return {str(key): json_compatible(item) for key, item in value.items()}
    if is_dataclass(value) and not isinstance(value, type):
        return json_compatible(asdict(value))
    if isinstance(value, (list, tuple)):
        return [json_compatible(item) for item in value]
    if isinstance(value, (set, frozenset)):
        normalized = [json_compatible(item) for item in value]
        return sorted(
            normalized,
            key=lambda item: json.dumps(
                item,
                ensure_ascii=False,
                allow_nan=False,
                sort_keys=True,
                separators=(",", ":"),
            ),
        )
    return value


def canonical_json_bytes(value: Any) -> bytes:
    """Return deterministic finite UTF-8 JSON for hashing derived values."""

    return json.dumps(
        json_compatible(value),
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
