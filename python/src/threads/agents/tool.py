"""`tool()`: an app tool whose input type is a Pydantic model (spec/api.json `tool`).

The model is the tool's one schema: its JSON Schema is the pinned `input_schema` the model sees,
and the same model parses the arguments at the boundary (strict, JSON mode). There is no second
validator to drift from it.
"""

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Literal, Required, TypedDict, Unpack

from pydantic import BaseModel, JsonValue, TypeAdapter, ValidationError
from pydantic_core import to_json

from threads.agents.config import ConfigError
from threads.agents.context import RunContext
from threads.log import EffectClass, JsonObject, ToolSpec
from threads.log.jcs import canonicalize
from threads.loop.model import Found, LookupResult, NotFound, NotFoundNonfinal
from threads.loop.tools import Output
from threads.result import Ok

_OBJECT: TypeAdapter[dict[str, JsonValue]] = TypeAdapter(dict[str, JsonValue])


@dataclass(frozen=True, slots=True)
class Reconcile[O, D]:
    """How a reconcilable tool finds out whether its effect happened."""

    lookup: Callable[[str, RunContext[D]], Awaitable[LookupResult[O]]]
    finality: Literal["final", "nonfinal"]


class ToolOptions[I: BaseModel, O, D](TypedDict, total=False):
    name: Required[str]
    description: Required[str]
    input: Required[type[I]]
    runs: Literal["host", "sandbox"]
    execute: Callable[[I, RunContext[D]], Awaitable[O]]
    effect: EffectClass
    dedup_window_ms: int
    reconcile: Reconcile[O, D]
    ends_turn: bool


@dataclass(frozen=True, slots=True)
class Tool[I: BaseModel, O, D]:
    """An app tool (spec/api.json `Tool`). Build it with `tool()`."""

    name: str
    description: str
    input: type[I]
    execute: Callable[[I, RunContext[D]], Awaitable[O]]
    effect: EffectClass = "unguarded"
    dedup_window_ms: int | None = None
    reconcile: Reconcile[O, D] | None = None
    ends_turn: bool = False

    def spec(self) -> ToolSpec:
        """The pinned ToolSpec: what line 0 shows and what decides the effect class."""
        data: dict[str, JsonValue] = {
            "name": self.name,
            "description": self.description,
            "input_schema": json_schema(self.input),
            "effect_class": self.effect,
        }
        if self.dedup_window_ms is not None:
            data["dedup_window_ms"] = self.dedup_window_ms
        if self.ends_turn:
            data["ends_turn"] = True
        return ToolSpec.model_validate(data)

    def invalid(self, input: JsonObject) -> str | None:
        """Why the model's arguments fail the input model, or None."""
        why = invalid(self.input, dict(input))
        return None if why is None else f"invalid arguments for {self.name}: {why}"

    async def run(self, input: JsonObject, ctx: RunContext[D]) -> Output:
        """Runs the body. Its value is the result; an error it raises is an error result the
        model sees (spec/api.json `tool.execute`)."""
        text = canonicalize(dict(input))
        if not isinstance(text, Ok):
            return Output("the arguments are not canonical JSON", is_error=True)
        parsed = self.input.model_validate_json(text.value, strict=True)
        try:
            value = await self.execute(parsed, ctx)
        except Exception as error:
            # The tool's own failure is a result the model sees, not a crash of the run.
            return Output(f"{type(error).__name__}: {error}", is_error=True)
        return Output(render_value(value))

    async def lookup(self, effect_key: str, ctx: RunContext[D]) -> LookupResult[str]:
        """Reconciliation through the tool's own lookup; a `found` value becomes result text."""
        if self.reconcile is None:
            raise AssertionError("only a reconcilable tool is looked up")
        answer = await self.reconcile.lookup(effect_key, ctx)
        if isinstance(answer, Found):
            return Found(render_value(answer.value))
        if isinstance(answer, NotFound) and self.reconcile.finality == "nonfinal":
            # Finality is the tool's declared capability, never inferred from an answer.
            return NotFoundNonfinal()
        return answer


def json_schema(model: type[BaseModel]) -> dict[str, JsonValue]:
    """The model's JSON Schema as pinned: a tool's input_schema, an agent's output schema."""
    return _OBJECT.validate_python(model.model_json_schema())


def invalid(model: type[BaseModel], value: JsonValue) -> str | None:
    """Why a value the model sent fails `model`, or None. Strict and in JSON mode: `"1"` is not
    an int. One check for tool arguments and final_output candidates alike."""
    text = canonicalize(value)
    if not isinstance(text, Ok):
        return "not canonical JSON"
    try:
        model.model_validate_json(text.value, strict=True)
    except ValidationError as error:
        return f"{error.error_count()} error(s): {error}"
    return None


def render_value(value: object) -> str:
    """A tool's value as the text the model sees: a string as is, anything else as JSON."""
    if isinstance(value, str):
        return value
    if isinstance(value, BaseModel):
        return value.model_dump_json()
    return to_json(value).decode("utf-8")


def tool[I: BaseModel, O, D](**options: Unpack[ToolOptions[I, O, D]]) -> Tool[I, O, D]:
    """spec/api.json `tool`. `runs` defaults to "host" and is not pinned. Raises ConfigError for
    a definition that can't run: a sandbox tool (no sandbox in this build), an idempotent tool
    without its dedup window, or a reconcilable tool without its lookup."""
    effect = options.get("effect", "unguarded")
    execute = options.get("execute")
    if options.get("runs", "host") != "host" or execute is None:
        raise ConfigError("capability_missing", f"{options['name']}: only runs='host' with execute")
    window = options.get("dedup_window_ms")
    if (effect == "idempotent") != (window is not None):
        raise ConfigError("invalid_config", "dedup_window_ms goes with effect='idempotent' only")
    reconcile = options.get("reconcile")
    if (effect == "reconcilable") != (reconcile is not None):
        raise ConfigError("invalid_config", "reconcile goes with effect='reconcilable' only")
    return Tool(
        options["name"],
        options["description"],
        options["input"],
        execute,
        effect,
        window,
        reconcile,
        options.get("ends_turn", False),
    )
