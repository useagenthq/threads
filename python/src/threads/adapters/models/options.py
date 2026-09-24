"""What every model factory takes, and the `ModelInfo` it declares from them.

Limits come from the adapter's exact-id catalog (`catalog.py`), each overridable by an option; a
model the catalog doesn't list needs both limits. Nothing is guessed from a model name: a wrong
guess would silently break budgets and the context ladder.
"""

from collections.abc import Collection, Mapping
from dataclasses import dataclass
from typing import Final, Required, TypedDict

from pydantic import JsonValue

from threads._generated.model_catalog_v1 import Entry, WithdrawnItem
from threads.adapters.models.catalog import CATALOGS, ModelCatalog
from threads.agents.config import ConfigError
from threads.log import AdapterRef, ModelRef, Price
from threads.log import Model as ModelLimits
from threads.loop.model import ModelInfo
from threads.secrets import Secret

ACCEPTS: Final = ("text", "image_ref", "document_ref")
"""Every adapter here encodes images and documents; none takes recorded audio."""

DEFAULT_MAX_TOKENS: Final = 8192
"""The default per-request output cap. A long answer continues in a new request
(`max_output_continuations`), so no request reserves the model's whole output budget."""
_CAPS: Final = frozenset({"max_tokens", "max_output_tokens"})
"""Cap keys: the one way to set the cap is the max_tokens option."""


class ProviderOptions(TypedDict, total=False):
    params: Mapping[str, JsonValue]
    """Other provider request fields, pinned in Render v1 line 0."""
    price: Price
    api_key: str | Secret
    """Defaults to the factory's `secret("<ENV>")`, resolved at setup. Never pinned or logged."""
    base_url: str
    max_tokens: int
    """The per-request output cap, pinned as `params.max_tokens`. Defaults to
    min(8192, max_output_tokens)."""


class ModelOptions(ProviderOptions, total=False):
    """A catalog-backed factory's options: the limits default from its catalog."""

    max_input_tokens: int
    """The most input tokens one request may carry. Pinned as policy.models[].context_window."""
    max_output_tokens: int
    """The most output tokens the model can produce in one response."""


class ExplicitOptions(ProviderOptions, total=False):
    """A factory with no catalog: both limits are required."""

    max_input_tokens: Required[int]
    max_output_tokens: Required[int]


@dataclass(frozen=True, slots=True)
class Limits:
    max_input_tokens: int
    max_output_tokens: int
    max_tokens: int


def resolve_limits(
    factory: str,
    model: str,
    options: ModelOptions | ExplicitOptions,
    catalog: ModelCatalog | None = None,
) -> Limits:
    """`factory`'s limits for `model`: the options, else its catalog entry (the embedded one for
    `factory` unless `catalog` is given). Raises invalid_config naming what to pass."""
    catalog = catalog or CATALOGS.get(factory)
    entry = next((e for e in catalog.entries if e.id == model), None) if catalog else None
    withdrawal = next((w for w in catalog.withdrawn if w.id == model), None) if catalog else None
    known = None if withdrawal else entry
    inp = options.get("max_input_tokens", known.max_input_tokens if known else None)
    out = options.get("max_output_tokens", known.max_output_tokens if known else None)
    if inp is None or out is None:
        missing = " and ".join(
            n for n, v in (("max_input_tokens", inp), ("max_output_tokens", out)) if v is None
        )
        why = f'unknown model "{model}"; pass {missing}'
        if entry and withdrawal:
            why = _withdrawn(model, missing, entry, withdrawal)
        raise ConfigError("invalid_config", f"{factory}: {why}")
    cap = options.get("max_tokens", min(DEFAULT_MAX_TOKENS, out))
    for name, value in (("max_input_tokens", inp), ("max_output_tokens", out), ("max_tokens", cap)):
        # Exactly int: a bool is an int to Python, and an untyped caller may pass a float.
        if type(value) is not int or value < 1:
            raise ConfigError(
                "invalid_config",
                f"{factory}: {name} must be a positive whole number of tokens, not {value!r}",
            )
    if cap > out:
        raise ConfigError(
            "invalid_config",
            f"{factory}: max_tokens {cap} is above max_output_tokens {out}; lower it",
        )
    return Limits(inp, out, cap)


def _withdrawn(model: str, missing: str, entry: Entry, withdrawal: WithdrawnItem) -> str:
    """What to pass for a withdrawn id: the corrected values, or the original ones that keep a
    thread started with them continuing (its config_hash then matches)."""
    return (
        f'model "{model}" was withdrawn from the catalog ({withdrawal.reason}); pass {missing}: '
        f"max_input_tokens={withdrawal.use.max_input_tokens} and "
        f"max_output_tokens={withdrawal.use.max_output_tokens} are correct, and "
        f"max_input_tokens={entry.max_input_tokens} and max_output_tokens="
        f"{entry.max_output_tokens} continue threads started with the old values"
    )


def info(
    ref: ModelRef,
    adapter: AdapterRef,
    options: ModelOptions | ExplicitOptions,
    reserved: Collection[str],
    hosted: tuple[str, ...] = (),
) -> ModelInfo:
    """`params` can't set what the adapter derives (`reserved`), so line 0 pins exactly what is
    sent. No adapter here offers response lookup by client request id, so lookup is `none`."""
    given = options.get("params", {})
    clash = next((key for key in reserved if key in given), None)
    if clash is not None:
        fix = "pass max_tokens" if clash in _CAPS else "the adapter derives it from the render"
        raise ConfigError("invalid_config", f"{adapter.name} params can't set {clash}: {fix}")
    limits = resolve_limits(ref.provider, ref.name, options)
    declared = ModelLimits(
        provider=ref.provider,
        name=ref.name,
        context_window=limits.max_input_tokens,
        max_output_tokens=limits.max_output_tokens,
        input_billing_bound="context_window",
    )
    price = options.get("price")
    if price is not None:
        declared = declared.model_copy(update={"price": price})
    params = {**given, "max_tokens": limits.max_tokens}
    return ModelInfo(ref, adapter, params, declared, "none", ACCEPTS, hosted)
