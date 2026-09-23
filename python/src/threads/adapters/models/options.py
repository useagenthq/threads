"""What every model factory takes, and the `ModelInfo` it declares from them.

The window and output cap are declared by the caller, never guessed from a model name: a wrong
guess would silently break budgets and the context ladder.
"""

from collections.abc import Mapping
from typing import Final, NotRequired, Required, TypedDict

from pydantic import JsonValue

from threads.log import AdapterRef, ModelRef, Price
from threads.log import Model as ModelLimits
from threads.loop.model import ModelInfo

ACCEPTS: Final = ("text", "image_ref", "document_ref")
"""Every adapter here encodes images and documents; none takes recorded audio."""


class ModelOptions(TypedDict, total=False):
    context_window: Required[int]
    max_output_tokens: Required[int]
    params: NotRequired[Mapping[str, JsonValue]]
    """Provider request fields, pinned in Render v1 line 0 over the adapter's defaults."""
    price: NotRequired[Price]
    api_key: NotRequired[str]
    """Falls back to the SDK's environment variable."""
    base_url: NotRequired[str]


def info(
    ref: ModelRef,
    adapter: AdapterRef,
    options: ModelOptions,
    defaults: Mapping[str, JsonValue],
) -> ModelInfo:
    """No adapter here offers response lookup by client request id, so lookup is `none`."""
    limits = ModelLimits(
        provider=ref.provider,
        name=ref.name,
        context_window=options["context_window"],
        max_output_tokens=options["max_output_tokens"],
        input_billing_bound="context_window",
    )
    price = options.get("price")
    if price is not None:
        limits = limits.model_copy(update={"price": price})
    params = {**defaults, **options.get("params", {})}
    return ModelInfo(ref, adapter, params, limits, "none", ACCEPTS)
