"""canonicalize(): RFC 8785 vectors, rejection cases, and round-trip/idempotence properties."""

import struct
from decimal import localcontext

import pytest
from hypothesis import given
from hypothesis import strategies as st
from pydantic import JsonValue

from threads.log.jcs import MAX_SAFE_INTEGER, canonicalize
from threads.log.strict_json import parse_json
from threads.result import Err, Ok


def from_bits(bits: str) -> float:
    value: float = struct.unpack(">d", bytes.fromhex(bits))[0]
    return value


# RFC 8785 Appendix B: IEEE-754 bit patterns and their ECMAScript serialization.
APPENDIX_B = [
    ("0000000000000000", "0"),
    ("8000000000000000", "0"),
    ("0000000000000001", "5e-324"),
    ("8000000000000001", "-5e-324"),
    ("7fefffffffffffff", "1.7976931348623157e+308"),
    ("ffefffffffffffff", "-1.7976931348623157e+308"),
    ("4340000000000000", "9007199254740992"),
    ("c340000000000000", "-9007199254740992"),
    ("4430000000000000", "295147905179352830000"),
    ("44b52d02c7e14af5", "9.999999999999997e+22"),
    ("44b52d02c7e14af6", "1e+23"),
    ("44b52d02c7e14af7", "1.0000000000000001e+23"),
    ("444b1ae4d6e2ef4e", "999999999999999700000"),
    ("444b1ae4d6e2ef4f", "999999999999999900000"),
    ("444b1ae4d6e2ef50", "1e+21"),
    ("3eb0c6f7a0b5ed8c", "9.999999999999997e-7"),
    ("3eb0c6f7a0b5ed8d", "0.000001"),
    ("41b3de4355555553", "333333333.3333332"),
    ("41b3de4355555554", "333333333.33333325"),
    ("41b3de4355555555", "333333333.3333333"),
    ("41b3de4355555556", "333333333.3333334"),
    ("41b3de4355555557", "333333333.33333343"),
    ("becbf647612f3696", "-0.0000033333333333333333"),
    ("43143ff3c1cb0959", "1424953923781206.2"),
]


@pytest.mark.parametrize(("bits", "expected"), APPENDIX_B)
def test_rfc8785_number_vectors(bits: str, expected: str) -> None:
    assert canonicalize(from_bits(bits)) == Ok(expected)


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (1e21, "1e+21"),
        (1e20, "100000000000000000000"),
        (1e-7, "1e-7"),
        (1.5, "1.5"),
        (2.0, "2"),
        (-0.0, "0"),
        (MAX_SAFE_INTEGER, "9007199254740991"),
        ('\u20ac$\u000f\nA\'B"\\\\"/', '"€$\\u000f\\nA\'B\\"\\\\\\\\\\"/"'),
        ("\u2028\x7f\b\f\r\t", '"\u2028\x7f\\b\\f\\r\\t"'),
        (
            {"b": [1, None, True], "a": {"d": False, "c": "x"}},
            '{"a":{"c":"x","d":false},"b":[1,null,true]}',
        ),
        # UTF-16 order: U+1F600 (surrogates D83D DE00) sorts before U+E000, and "\r" before "1".
        ({"\ue000": 1, "\U0001f600": 2, "1": 3, "\r": 4}, '{"\\r":4,"1":3,"😀":2,"\ue000":1}'),
    ],
)
def test_canonical_text(value: JsonValue, expected: str) -> None:
    assert canonicalize(value) == Ok(expected)


@pytest.mark.parametrize(
    "value",
    [
        float("nan"),
        float("inf"),
        float("-inf"),
        from_bits("7fffffffffffffff"),
        MAX_SAFE_INTEGER + 1,
        -MAX_SAFE_INTEGER - 1,
        "\ud800",
        {"\udfff": 1},
        [{"x": ["a\udc00"]}],
    ],
)
def test_uncanonicalizable_values_are_errors(value: JsonValue) -> None:
    assert isinstance(canonicalize(value), Err)


SAFE_INTS = st.integers(min_value=-MAX_SAFE_INTEGER, max_value=MAX_SAFE_INTEGER)
TEXT = st.text(st.characters(exclude_categories=("Cs",)))
FLOATS = st.floats(allow_nan=False, allow_infinity=False).filter(
    lambda f: not (f.is_integer() and abs(f) > MAX_SAFE_INTEGER)
)
JSON_VALUES: st.SearchStrategy[JsonValue] = st.recursive(
    st.none() | st.booleans() | SAFE_INTS | FLOATS | TEXT,
    lambda children: st.lists(children) | st.dictionaries(TEXT, children),
    max_leaves=30,
)


@given(JSON_VALUES)
def test_round_trip_and_idempotence(value: JsonValue) -> None:
    text = canonicalize(value)
    assert isinstance(text, Ok)
    parsed = parse_json(text.value)
    assert isinstance(parsed, Ok)
    assert parsed.value == value  # Python equality: 2.0 == 2 and -0.0 == 0
    assert canonicalize(parsed.value) == text


@given(st.floats(allow_nan=False, allow_infinity=False))
def test_numbers_round_trip_exactly(value: float) -> None:
    text = canonicalize(value)
    assert isinstance(text, Ok)
    assert float(text.value) == value


def test_numbers_ignore_the_callers_decimal_context() -> None:
    # Canonical bytes feed hashes, so application decimal settings must not change them.
    with localcontext() as context:
        context.prec = 6
        assert canonicalize(0.123456789) == Ok("0.123456789")
        assert canonicalize(123456789012.0) == Ok("123456789012")
