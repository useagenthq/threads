"""One MCP tool as an agent tool: namespaced `mcp__<server>__<tool>`, its input
checked against the server's JSON Schema with a real validator, its call on the effect path, and
its output framed as untrusted reference."""

from dataclasses import dataclass
from datetime import timedelta
from http import HTTPStatus
from typing import Final

from jsonschema import Draft202012Validator, validate
from jsonschema import ValidationError as SchemaValidationError
from mcp import ClientSession
from mcp.shared.exceptions import McpError
from mcp.types import (
    CONNECTION_CLOSED,
    CallToolResult,
    ReadResourceResult,
    TextContent,
    TextResourceContents,
)
from pydantic import AnyUrl, JsonValue

from threads.adapters.mcp.transport import FENCE_REFUSED
from threads.agents.context import RunContext
from threads.log import EffectClass, JsonObject, ToolSpec
from threads.loop.model import LookupResult, LookupUnknown
from threads.loop.tools import Dispatched, NotSent, Output, Uncertain
from threads.render.framing import reference

CALL_TIMEOUT: Final = timedelta(seconds=120)
READ_RESOURCE: Final = "read_resource"
READ_RESOURCE_SCHEMA: Final[dict[str, JsonValue]] = {
    "type": "object",
    "properties": {"uri": {"type": "string", "minLength": 1}},
    "required": ["uri"],
    "additionalProperties": False,
}


@dataclass(frozen=True, slots=True)
class McpTool:
    """A resolved MCP tool, bound to its live session for one run."""

    name: str
    remote: str
    """The tool's own name on the server."""
    description: str
    input_schema: dict[str, JsonValue]
    effect: EffectClass
    session: ClientSession
    resource: bool = False
    """mcp__<server>__read_resource: reads a resource by URI."""

    def spec(self) -> ToolSpec:
        return ToolSpec.model_validate(
            {
                "name": self.name,
                "description": self.description,
                "input_schema": self.input_schema,
                "effect_class": self.effect,
            }
        )

    def invalid(self, input: JsonObject) -> str | None:
        try:
            validate(dict(input), self.input_schema, cls=Draft202012Validator)
        except SchemaValidationError as error:
            return f"invalid arguments for {self.name}: {error.message}"
        return None

    async def run(self, input: JsonObject, ctx: RunContext[object]) -> Dispatched:
        try:
            if self.resource:
                read = await self.session.read_resource(AnyUrl(str(input["uri"])))
                return _contents(self.name, read)
            result = await self.session.call_tool(self.remote, dict(input), CALL_TIMEOUT)
        except McpError as error:
            return self._answered(error)
        except TimeoutError:
            return Uncertain("timeout")
        except Exception:
            # Anything else after the request left: it may have run.
            return Uncertain("transport_error")
        return _output(self.name, result)

    def _answered(self, error: McpError) -> Dispatched:
        match error.error.code:
            case code if code == FENCE_REFUSED:
                return NotSent()
            case code if code == HTTPStatus.REQUEST_TIMEOUT:
                return Uncertain("timeout")
            case code if code == CONNECTION_CLOSED:
                return Uncertain("transport_error")
            case code:
                # The server answered with an error: a final answer the model sees.
                return Output(f"{self.name}: error {code}: {error.error.message}", True)

    async def lookup(self, effect_key: str, ctx: RunContext[object]) -> LookupResult[str]:
        return LookupUnknown(f"{self.name} has no reconcile contract")


def _output(name: str, result: CallToolResult) -> Output:
    """The server's content is data the model reads, never instructions: framed as reference."""
    parts = [
        c.text if isinstance(c, TextContent) else f"[{c.type} content omitted]"
        for c in result.content
    ]
    return Output(reference("mcp", name, "\n".join(parts)), is_error=result.isError)


def _contents(name: str, result: ReadResourceResult) -> Output:
    parts = [
        c.text if isinstance(c, TextResourceContents) else "[blob content omitted]"
        for c in result.contents
    ]
    return Output(reference("mcp", name, "\n".join(parts)), is_error=False)
