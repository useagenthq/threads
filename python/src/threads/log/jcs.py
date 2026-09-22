"""RFC 8785 (JCS) canonical JSON: the byte form of every log line and structured hash input.

Keys sort by UTF-16 code units, numbers use the ECMAScript Number-to-String algorithm, strings
use the minimal JSON escapes, and there is no whitespace. Values JCS can't represent (non-finite
numbers, lone surrogates) and integers TypeScript can't hold exactly are
errors, never silently changed.
"""

import json
import math
import re
from decimal import Decimal
from typing import assert_never

from pydantic import JsonValue

from threads.result import Err, Ok

MAX_SAFE_INTEGER = 2**53 - 1
MAX_DEPTH = 64
"""Levels of arrays and objects a line may nest, the line object being level 1 (wire rule 2)."""
_SURROGATE = re.compile("[\ud800-\udfff]")
# ECMAScript prints a number in fixed notation while its decimal exponent n is in (-6, 21].
_FIXED_MAX = 21
_FIXED_MIN = -6


class _NotCanonicalizableError(ValueError):
    pass


def canonicalize(value: JsonValue) -> Ok[str] | Err[str]:
    """Returns the RFC 8785 text of `value`. Encode it as UTF-8 for the canonical bytes."""
    parts: list[str] = []
    try:
        _write(value, parts, 1)
    except _NotCanonicalizableError as error:
        return Err(str(error))
    return Ok("".join(parts))


def has_lone_surrogate(text: str) -> bool:
    return _SURROGATE.search(text) is not None


def _write(value: JsonValue, out: list[str], depth: int) -> None:
    match value:
        case None:
            out.append("null")
        case bool():
            out.append("true" if value else "false")
        case int():
            out.append(_integer(value))
        case float():
            out.append(_number(value))
        case str():
            out.append(_string(value))
        case list():
            _write_array(value, out, _within_depth(depth))
        case dict():
            _write_object(value, out, _within_depth(depth))
        case _:
            assert_never(value)


def _write_array(items: list[JsonValue], out: list[str], depth: int) -> None:
    out.append("[")
    for index, item in enumerate(items):
        if index:
            out.append(",")
        _write(item, out, depth + 1)
    out.append("]")


def _write_object(obj: dict[str, JsonValue], out: list[str], depth: int) -> None:
    out.append("{")
    for index, key in enumerate(sorted(obj, key=_utf16_key)):
        if index:
            out.append(",")
        out.append(_string(key))
        out.append(":")
        _write(obj[key], out, depth + 1)
    out.append("}")


def _within_depth(depth: int) -> int:
    if depth > MAX_DEPTH:
        raise _NotCanonicalizableError(f"nesting deeper than {MAX_DEPTH} levels")
    return depth


def _utf16_key(key: str) -> bytes:
    # RFC 8785 §3.2.3 orders by UTF-16 code units, which differs from code point order above
    # the BMP (U+1F600 sorts before U+E000). Surrogates are rejected when the key is written.
    return key.encode("utf-16-be", "surrogatepass")


def _integer(value: int) -> str:
    if abs(value) > MAX_SAFE_INTEGER:
        raise _NotCanonicalizableError(f"integer {value} is outside the safe integer range")
    return str(value)


def _string(text: str) -> str:
    if has_lone_surrogate(text):
        raise _NotCanonicalizableError("lone surrogate in a string")
    # json.dumps with ensure_ascii=False emits exactly the RFC 8785 escapes: \" \\ \b \f \n \r
    # \t, lowercase \u00xx for other controls, and every other character as itself.
    return json.dumps(text, ensure_ascii=False)


def _number(value: float) -> str:
    """ECMAScript Number::toString for a finite double (ECMA-262 §6.1.6.1.20)."""
    if not math.isfinite(value):
        raise _NotCanonicalizableError(f"non-finite number {value!r}")
    if value == 0:
        return "0"  # -0 too
    sign = "-" if value < 0 else ""
    # repr gives the shortest digits that round-trip, which is the digit string ES requires.
    # Decimal(str) is exact; normalize() would round under the caller's decimal context.
    _, digit_tuple, exponent = Decimal(repr(abs(value))).as_tuple()
    if not isinstance(exponent, int):  # only NaN/Infinity have a string exponent
        raise AssertionError(value)
    digits = "".join(map(str, digit_tuple))
    stripped = digits.rstrip("0")
    exponent += len(digits) - len(stripped)
    return sign + _place_point(stripped, len(stripped) + exponent)


def _place_point(digits: str, n: int) -> str:
    """Formats value = 0.digits x 10^n the way ECMAScript does."""
    k = len(digits)
    if k <= n <= _FIXED_MAX:
        return digits + "0" * (n - k)
    if 0 < n <= _FIXED_MAX:
        return f"{digits[:n]}.{digits[n:]}"
    if _FIXED_MIN < n <= 0:
        return f"0.{'0' * -n}{digits}"
    exponent = n - 1
    mantissa = digits if k == 1 else f"{digits[0]}.{digits[1:]}"
    return f"{mantissa}e{'+' if exponent > 0 else '-'}{abs(exponent)}"
