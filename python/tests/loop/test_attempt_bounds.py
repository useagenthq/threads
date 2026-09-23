"""An attempt's per-limit bounds (spec/schema/README.md, Budget enforcement): only a JSON integer
max_tokens bounds the output. A boolean is not one, though Python's bool is an int; TS agrees."""

import pytest
from pydantic import JsonValue
from pydantic.experimental.missing_sentinel import MISSING

from threads.log import Model
from threads.loop.budget import bounds
from threads.reduce.projections import bound

MODEL = Model.model_validate(
    {
        "provider": "scripted",
        "name": "scripted-1",
        "context_window": 200_000,
        "max_output_tokens": 8192,
        "input_billing_bound": "context_window",
        "price": {"input": 3000, "output": 15_000},
    }
)


@pytest.mark.parametrize("max_tokens", [True, False, 1.5, "1024", None])
def test_a_max_tokens_that_is_not_an_integer_bounds_nothing(max_tokens: JsonValue) -> None:
    assert bound(MODEL, max_tokens, MISSING) is None
    limits = bounds(MODEL, max_tokens)
    assert limits["max_output_tokens"] is None
    assert limits["max_cost_nanos"] is None


def test_an_integer_max_tokens_bounds_the_output_and_the_cost() -> None:
    limits = bounds(MODEL, 1024)
    assert limits["max_output_tokens"] == 1024  # noqa: PLR2004 - the value passed in
    assert limits["max_cost_nanos"] == 200_000 * 3000 + 1024 * 15_000
