# pyright: strict
"""Restricted RFC 8785 canonical JSON for fixture generation.

Implements null, booleans, integers within +-(2^53-1), strings without lone
surrogates, arrays and objects, with keys sorted recursively by UTF-16 code units.
It refuses floats (ECMAScript number spelling is not implemented), so it is not a
full JCS implementation and must not be reused as one.
"""

from __future__ import annotations

import json

type JsonValue = bool | int | float | str | list[JsonValue] | dict[str, JsonValue] | None
type Obj = dict[str, JsonValue]

SAFE_INT = 2**53 - 1


def _utf16_key(k: str) -> bytes:
    return k.encode("utf-16-be")


def _string(s: str) -> str:
    try:
        s.encode("utf-8")
    except UnicodeEncodeError as err:
        raise ValueError("lone surrogate in string") from err
    # json.dumps(ensure_ascii=False) escapes exactly what RFC 8785 escapes for strings.
    return json.dumps(s, ensure_ascii=False)


def _canon(v: JsonValue) -> str:
    if v is None:
        return "null"
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, int):
        if abs(v) > SAFE_INT:
            raise ValueError("integer outside the safe band")
        return str(v)
    if isinstance(v, float):
        raise TypeError("floats are outside this generator's canonical subset")
    if isinstance(v, str):
        return _string(v)
    if isinstance(v, list):
        return "[" + ",".join(_canon(x) for x in v) + "]"
    keys = sorted(v, key=_utf16_key)
    return "{" + ",".join(_string(k) + ":" + _canon(v[k]) for k in keys) + "}"


def canonical(v: JsonValue) -> bytes:
    return _canon(v).encode("utf-8")


def selftest() -> None:
    # U+1F600 is the surrogate pair D83D DE00, which sorts before U+E000 in UTF-16
    # order even though its code point is larger.
    vectors: list[tuple[JsonValue, bytes]] = [
        ({"\ue000": 1, "\U0001f600": 2}, '{"\U0001f600":2,"\ue000":1}'.encode()),
        (
            {"b": [1, {"d": None, "c": True}], "a": "\n\u001f"},
            b'{"a":"\\n\\u001f","b":[1,{"c":true,"d":null}]}',
        ),
    ]
    for value, want in vectors:
        if canonical(value) != want:
            raise AssertionError(f"canonical({value!r}) != {want!r}")
    for bad in (1.5, 2**53, "\ud800"):
        try:
            canonical(bad)
        except (TypeError, ValueError):
            continue
        raise AssertionError(f"accepted {bad!r}")
