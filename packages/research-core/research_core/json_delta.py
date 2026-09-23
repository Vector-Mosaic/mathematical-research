"""Neutral deterministic JSON Pointer resolution and whole-tree delta facts."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping, Sequence

from .research_model import deep_freeze, deep_thaw


class JsonPointerError(ValueError):
    """One exact invalid JSON Pointer or traversal fact."""

    def __init__(self, location: str, message: str) -> None:
        super().__init__(message)
        self.code = "json_pointer_invalid"
        self.location = location
        self.message = message


@dataclass(frozen=True, slots=True)
class JsonPointerDelta:
    pointer: str
    change: str
    identity: str | None
    before_present: bool
    after_present: bool
    before: Any = None
    after: Any = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "before", deep_freeze(self.before))
        object.__setattr__(self, "after", deep_freeze(self.after))

    def to_mapping(self) -> dict[str, Any]:
        return {
            "pointer": self.pointer,
            "change": self.change,
            "identity": self.identity,
            "before_present": self.before_present,
            "after_present": self.after_present,
            "before": deep_thaw(self.before),
            "after": deep_thaw(self.after),
        }


def _error(location: str, message: str) -> JsonPointerError:
    return JsonPointerError(location, message)


def _decode_pointer_segment(segment: str) -> str:
    result: list[str] = []
    index = 0
    while index < len(segment):
        if segment[index] != "~":
            result.append(segment[index])
            index += 1
            continue
        if index + 1 >= len(segment) or segment[index + 1] not in {"0", "1"}:
            raise _error(segment, "invalid JSON Pointer escape")
        result.append("~" if segment[index + 1] == "0" else "/")
        index += 2
    return "".join(result)


def _encode_pointer_segment(segment: str) -> str:
    return segment.replace("~", "~0").replace("/", "~1")


def resolve_json_pointer(document: Any, pointer: str) -> Any:
    """Resolve deterministic JSON Pointer with exact ``@id=...`` selectors."""

    if pointer == "":
        return document
    if not isinstance(pointer, str) or not pointer.startswith("/"):
        raise _error(str(pointer), "pointer must be empty or start with /")
    current = document
    for raw_segment in pointer[1:].split("/"):
        segment = _decode_pointer_segment(raw_segment)
        if segment.startswith("@id="):
            if isinstance(current, (str, bytes, bytearray)) or not isinstance(
                current, Sequence
            ):
                raise _error(pointer, "ID selector requires an array")
            wanted = segment[4:]
            matches = [
                item
                for item in current
                if isinstance(item, Mapping) and item.get("id") == wanted
            ]
            if len(matches) != 1:
                raise _error(
                    pointer,
                    f"ID selector {wanted!r} matched {len(matches)} records",
                )
            current = matches[0]
        elif isinstance(current, Mapping):
            if segment not in current:
                raise _error(pointer, f"missing object key {segment!r}")
            current = current[segment]
        elif not isinstance(current, (str, bytes, bytearray)) and isinstance(
            current, Sequence
        ):
            if not segment.isdigit():
                raise _error(pointer, "array segment must be an index")
            index = int(segment)
            if index >= len(current):
                raise _error(pointer, "array index is out of range")
            current = current[index]
        else:
            raise _error(pointer, "pointer traverses a scalar")
    return current


def _id_index(value: Sequence[Any]) -> dict[str, Mapping[str, Any]] | None:
    result: dict[str, Mapping[str, Any]] = {}
    for item in value:
        if not isinstance(item, Mapping):
            return None
        identity = item.get("id")
        if not isinstance(identity, str) or not identity or identity in result:
            return None
        result[identity] = item
    return result


def compare_json_pointer_delta(before: Any, after: Any) -> tuple[JsonPointerDelta, ...]:
    """Compare whole JSON trees, keying exact record arrays by ``id``."""

    deltas: list[JsonPointerDelta] = []

    def walk(left: Any, right: Any, pointer: str, identity: str | None) -> None:
        if isinstance(left, Mapping) and isinstance(right, Mapping):
            inherited = right.get("id", left.get("id", identity))
            if not isinstance(inherited, str):
                inherited = identity
            for key in sorted(set(left) | set(right)):
                child = f"{pointer}/{_encode_pointer_segment(str(key))}"
                if key not in left:
                    value = right[key]
                    added_identity = (
                        value.get("id")
                        if isinstance(value, Mapping)
                        and isinstance(value.get("id"), str)
                        else inherited
                    )
                    deltas.append(
                        JsonPointerDelta(
                            child,
                            "added",
                            added_identity,
                            False,
                            True,
                            None,
                            value,
                        )
                    )
                elif key not in right:
                    value = left[key]
                    removed_identity = (
                        value.get("id")
                        if isinstance(value, Mapping)
                        and isinstance(value.get("id"), str)
                        else inherited
                    )
                    deltas.append(
                        JsonPointerDelta(
                            child,
                            "removed",
                            removed_identity,
                            True,
                            False,
                            value,
                            None,
                        )
                    )
                else:
                    walk(left[key], right[key], child, inherited)
            return
        if (
            not isinstance(left, (str, bytes, bytearray))
            and not isinstance(right, (str, bytes, bytearray))
            and isinstance(left, Sequence)
            and isinstance(right, Sequence)
        ):
            left_index = _id_index(left)
            right_index = _id_index(right)
            if (left_index is not None and right_index is not None) and (
                left_index or right_index
            ):
                for record_id in sorted(set(left_index) | set(right_index)):
                    child = f"{pointer}/@id={_encode_pointer_segment(record_id)}"
                    if record_id not in left_index:
                        deltas.append(
                            JsonPointerDelta(
                                child,
                                "added",
                                record_id,
                                False,
                                True,
                                None,
                                right_index[record_id],
                            )
                        )
                    elif record_id not in right_index:
                        deltas.append(
                            JsonPointerDelta(
                                child,
                                "removed",
                                record_id,
                                True,
                                False,
                                left_index[record_id],
                                None,
                            )
                        )
                    else:
                        walk(
                            left_index[record_id],
                            right_index[record_id],
                            child,
                            record_id,
                        )
                return
            if list(left) != list(right):
                deltas.append(
                    JsonPointerDelta(
                        pointer,
                        "modified",
                        identity,
                        True,
                        True,
                        left,
                        right,
                    )
                )
            return
        if left != right:
            deltas.append(
                JsonPointerDelta(
                    pointer,
                    "modified",
                    identity,
                    True,
                    True,
                    left,
                    right,
                )
            )

    walk(before, after, "", None)
    return tuple(sorted(deltas, key=lambda item: item.pointer))


__all__ = [
    "JsonPointerDelta",
    "JsonPointerError",
    "compare_json_pointer_delta",
    "resolve_json_pointer",
]
