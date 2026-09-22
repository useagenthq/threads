"""A dispatch table from event class to its `validate_next` rule and fold step.

Each handler checks its rules first and mutates the fold only when they hold, so a rejected
event leaves the fold as it was. Event types without an entry change nothing but the envelope.
"""

from collections.abc import Callable

from pydantic import BaseModel, JsonValue, TypeAdapter

from threads.log import Event, ParseError
from threads.reduce.fold import Fold

type Handler = Callable[[Fold, Event], ParseError | None]

_JSON: TypeAdapter[JsonValue] = TypeAdapter(JsonValue)


def on[E](cls: type[E], step: Callable[[Fold, E], ParseError | None]) -> tuple[type, Handler]:
    """Binds a handler typed for one event class into the table's uniform signature."""

    def run(fold: Fold, event: Event) -> ParseError | None:
        if not isinstance(event, cls):
            raise TypeError(f"{type(event).__name__} dispatched to the {cls.__name__} handler")
        return step(fold, event)

    return cls, run


def to_json(model: BaseModel) -> JsonValue:
    """A parsed model back as its wire JSON value (absent fields stay absent)."""
    return _JSON.validate_python(model.model_dump(mode="json", by_alias=True))
