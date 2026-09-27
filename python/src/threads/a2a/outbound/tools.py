"""`remote(...).tools()`: a partner's agent as two pinned host tools, so it spreads next to the
thread's own tools.

Two tools rather than one, because a tool's effect class is fixed when it is pinned: a read folded
into the send tool would carry the send's class and ask for approval to send nothing."""

import re
from dataclasses import dataclass
from typing import Annotated, ClassVar, Final, Literal

from pydantic import BaseModel, ConfigDict, Field, StrictStr, ValidationError

from threads.a2a.outbound.reconcile import reconcile_send
from threads.a2a.outbound.send import SendArgs, begin_send, run_send
from threads.a2a.outbound.status import run_status
from threads.a2a.protocol import Sending
from threads.a2a.remote import Remote
from threads.agents.config import ConfigError
from threads.log import JsonObject, ToolSpec
from threads.loop.model import LookupResult, LookupUnknown, NotFound, NotFoundNonfinal
from threads.loop.tools import Dispatched, Invocation, Output, Prepared, Refused, Termination

MAX_MESSAGE: Final = 16_384
"""16 KiB of text: a message, not a document. A partner's own limits are its own to enforce."""

_NAME: Final = re.compile(r"^[a-z][a-z0-9_]*$")

FINALITY: Final[Literal["final", "nonfinal"]] = "nonfinal"
"""What a lookup that finds nothing proves about an A2A send: nothing at all."""


class SendInput(BaseModel):
    """One message to the remote agent."""

    model_config: ClassVar[ConfigDict] = ConfigDict(extra="forbid", frozen=True)

    message: Annotated[StrictStr, Field(min_length=1, max_length=MAX_MESSAGE)]
    task_id: Annotated[
        StrictStr | None,
        Field(
            default=None,
            min_length=1,
            description="Continue a task that asked this conversation for input.",
        ),
    ] = None


class StatusInput(BaseModel):
    """Read a task this conversation already created. Sends no message."""

    model_config: ClassVar[ConfigDict] = ConfigDict(extra="forbid", frozen=True)

    task_id: Annotated[
        StrictStr, Field(min_length=1, description="A task this conversation created.")
    ]


def _schema(model: type[BaseModel]) -> JsonObject:
    schema = model.model_json_schema()
    schema.pop("$defs", None)
    return schema


@dataclass(frozen=True, slots=True)
class RemoteTool:
    """One of a remote's two tools, and the runner the loop dispatches it through. It holds no
    per-run state: everything it needs comes on the invocation, which carries the log."""

    remote: Remote
    kind: Literal["send", "status"]
    tool_name: str
    description: str

    @property
    def name(self) -> str:
        return self.tool_name

    def spec(self) -> ToolSpec:
        if self.kind == "send":
            # reconcilable, not idempotent: the dedup window comes from a partner's card, which is
            # not known when the tool is pinned, and pinning one would claim dedup we have not seen.
            return ToolSpec.model_validate(
                {
                    "name": self.tool_name,
                    "description": self.description,
                    "input_schema": _schema(SendInput),
                    "effect_class": "reconcilable",
                }
            )
        return ToolSpec.model_validate(
            {
                "name": self.tool_name,
                "description": self.description,
                "input_schema": _schema(StatusInput),
                "effect_class": "read_only",
            }
        )

    def _sending(self) -> Sending:
        """The credential is resolved here, at send time, and rides in the header only
        (invariant 4)."""
        auth = self.remote.auth
        return Sending(
            self.remote.timeout_ms,
            authorization=None if auth is None else f"Bearer {auth.reveal()}",
            transport=self.remote.transport,
            resolve=self.remote.resolve,
        )

    def invalid(self, spec: ToolSpec, input: JsonObject) -> str | None:
        model = SendInput if self.kind == "send" else StatusInput
        try:
            model.model_validate(dict(input), strict=True)
        except ValidationError as error:
            return f"invalid arguments for {spec.name}: {error.error_count()} error(s)"
        return None

    async def begin(self, call: Invocation) -> Prepared:
        if self.kind != "send":
            return ()
        try:
            args = SendInput.model_validate(dict(call.input), strict=True)
        except ValidationError:
            return Refused(f"invalid arguments for {self.tool_name}")
        return await begin_send(
            self.remote, SendArgs(args.message, args.task_id), call, self._sending()
        )

    async def dispatch(self, call: Invocation) -> Dispatched:
        if self.kind == "send":
            return await run_send(self.remote, call, self._sending())
        try:
            args = StatusInput.model_validate(dict(call.input), strict=True)
        except ValidationError:
            return Output(f"invalid arguments for {self.tool_name}", is_error=True)
        return await run_status(self.remote, args.task_id, call, self._sending())

    async def lookup(self, call: Invocation) -> LookupResult[str]:
        if self.kind != "send":
            return LookupUnknown(f"{self.tool_name} begins no effect")
        answer = await reconcile_send(self.remote, call, self._sending())
        # Finality is the tool's declared capability, never inferred from an answer, and A2A's is
        # nonfinal: "not found" may be a peer still creating the task, or a truncated history, so
        # it parks instead of freeing a re-send.
        if isinstance(answer, NotFound) and FINALITY == "nonfinal":
            return NotFoundNonfinal()
        return answer

    async def terminate(self, call: Invocation) -> Termination:
        # A remote call starts no sandbox process group, so none can be confirmed gone.
        return "unknown"

    def provider_now(self) -> int | None:
        return None


def remote_tools(remote: Remote, name: str, description: str) -> tuple[RemoteTool, RemoteTool]:
    if _NAME.match(name) is None:
        raise ConfigError("invalid_config", f"remote tool name {name!r} must match [a-z][a-z0-9_]*")
    status = (
        f"{description} This reads the state of a task {name} created and sends nothing. It"
        " records each state change it observes in this conversation's log."
    )
    return (
        RemoteTool(remote, "send", name, description),
        RemoteTool(remote, "status", f"{name}_status", status),
    )
