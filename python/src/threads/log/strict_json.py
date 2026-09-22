"""JSON decoding under the RFC 8785 admission rules.

`json.loads` alone accepts duplicate keys (last wins), `NaN`/`Infinity`, overflowing floats,
lone surrogates and integers TypeScript can't represent. Each of those would let the two
implementations read the same bytes differently, so they are rejected here.
"""

import json
import math
import re
from collections import Counter
from collections.abc import Sequence

from pydantic import JsonValue

from threads.result import Err, Ok

MAX_SAFE_INTEGER = 2**53 - 1
_SURROGATE = re.compile("[\ud800-\udfff]")


class _InadmissibleError(ValueError):
    pass


def parse_json(text: str) -> Ok[JsonValue] | Err[str]:
    try:
        value: JsonValue = json.loads(
            text,
            object_pairs_hook=_object_without_duplicates,
            parse_constant=_reject_constant,
            parse_float=_finite_float,
            parse_int=_safe_int,
        )
        _reject_lone_surrogates(value)
    except RecursionError:
        return Err("JSON nests too deeply")
    except ValueError as error:  # JSONDecodeError and _InadmissibleError
        return Err(str(error))
    return Ok(value)


def _object_without_duplicates(pairs: Sequence[tuple[str, JsonValue]]) -> dict[str, JsonValue]:
    obj = dict(pairs)
    if len(obj) != len(pairs):
        counts = Counter(key for key, _ in pairs)
        duplicate = next(key for key, count in counts.items() if count > 1)
        raise _InadmissibleError(f"duplicate key {duplicate!r}")
    return obj


def _reject_constant(name: str) -> float:
    raise _InadmissibleError(f"non-finite number {name}")


def _finite_float(text: str) -> float:
    value = float(text)
    if not math.isfinite(value):
        raise _InadmissibleError(f"non-finite number {text}")
    if value.is_integer() and abs(value) > MAX_SAFE_INTEGER:
        raise _InadmissibleError(f"integral value {text} is outside the safe integer range")
    return value


def _safe_int(text: str) -> int:
    value = int(text)
    if abs(value) > MAX_SAFE_INTEGER:
        raise _InadmissibleError(f"integer {text} is outside the safe integer range")
    return value


def _reject_lone_surrogates(value: JsonValue) -> None:
    # json.loads joins escaped surrogate pairs, so any surrogate left is unpaired.
    strings: list[str] = []
    stack: list[JsonValue] = [value]
    while stack:
        item = stack.pop()
        if isinstance(item, str):
            strings.append(item)
        elif isinstance(item, list):
            stack.extend(item)
        elif isinstance(item, dict):
            strings.extend(item)
            stack.extend(item.values())
    if any(_SURROGATE.search(s) for s in strings):
        raise _InadmissibleError("lone surrogate in a string")
