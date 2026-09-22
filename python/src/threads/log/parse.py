"""Parses one stored log line: the storage trust boundary (spec/schema/README.md, wire rules)."""

from dataclasses import dataclass
from typing import Annotated

from pydantic import Field, JsonValue, TypeAdapter, ValidationError

from threads._generated.events_v1 import ErrorCode, Event, Head, Header, KnownTag, UnknownEvent
from threads.log.strict_json import parse_json
from threads.result import Err, Ok

MAX_LINE_BYTES = 1 << 20

type LogLine = Header | Head | Event | UnknownEvent
"""A branch header, an event this version knows, an ignorable unknown event, or the head
checkpoint. Unknown critical events never parse: they refuse the log."""


@dataclass(frozen=True, slots=True)
class ParseError:
    code: ErrorCode
    message: str


_FRAMING: TypeAdapter[Header | Head] = TypeAdapter(
    Annotated[Header | Head, Field(discriminator="format")]
)
_EVENT: TypeAdapter[Event] = TypeAdapter(Event)


def parse_log_line(line: str) -> Ok[LogLine] | Err[ParseError]:
    """Parses a line without its trailing newline.

    Admission (duplicate keys, non-finite numbers, lone surrogates, unsafe integers, the 1 MiB
    cap) and schema failures are `invalid_line`. An event whose `(type, type_version)` this
    version doesn't know is kept when `critical` is false and is `unsupported_critical_event`
    otherwise.
    """
    if len(line.encode("utf-8", "surrogatepass")) > MAX_LINE_BYTES:
        return _invalid(f"line exceeds {MAX_LINE_BYTES} bytes")
    match parse_json(line):
        case Err(error=reason):
            return _invalid(reason)
        case Ok(value=value):
            return _parse_value(value)


def _parse_value(value: JsonValue) -> Ok[LogLine] | Err[ParseError]:
    if not isinstance(value, dict):
        return _invalid("a log line is a JSON object")
    try:
        if "format" in value:
            return Ok(_FRAMING.validate_python(value))
        if _is_known(value):
            return Ok(_EVENT.validate_python(value))
        unknown = UnknownEvent.model_validate(value)
    except ValidationError as error:
        return _invalid(str(error))
    if unknown.critical:
        return Err(
            ParseError(
                "unsupported_critical_event",
                f"critical event {unknown.type} v{unknown.type_version} is unknown to this reader",
            )
        )
    return Ok(unknown)


def _is_known(value: dict[str, JsonValue]) -> bool:
    # The schema's `not KnownTag` on UnknownEvent, checked with the schema's own KnownTag.
    tag = {"type": value.get("type"), "type_version": value.get("type_version")}
    try:
        KnownTag.model_validate(tag)
    except ValidationError:
        return False
    return True


def _invalid(message: str) -> Err[ParseError]:
    return Err(ParseError("invalid_line", message))
