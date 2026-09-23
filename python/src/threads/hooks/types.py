"""The hook set (spec/api.json `Hooks`). Every decision is a value of the wire
`hook_decision.decision` enum; a hook returns a wire-cased object, and injections ride on
`allow` / `proceed` as a separate list, never as a decision."""

from collections.abc import Awaitable, Callable, Mapping, Sequence
from typing import TYPE_CHECKING, Final, Literal, NotRequired, TypedDict

from threads.agents.context import RunContext
from threads.log import (
    AgentFinishedData,
    ModelResponseData,
    ModelSettings,
    Span,
    ToolCallData,
    ToolResultData,
    UserInputData,
)
from threads.log import Event as LogEvent
from threads.reduce.state import ReducedState

if TYPE_CHECKING:
    from threads.loop.runtime import RunErrorCode

type HookName = Literal[
    "session_start",
    "session_end",
    "before_input",
    "before_model",
    "after_model",
    "before_tool",
    "permission_request",
    "permission_denied",
    "after_tool",
    "before_tool_result",
    "after_tool_batch",
    "before_compact",
    "after_compact",
    "on_stop",
    "on_stop_failure",
    "subagent_start",
    "subagent_stop",
    "before_model_switch",
    "after_model_switch",
    "notification",
]
"""The API names; the wire `hook_decision.hook` differs only for on_stop_failure."""

type HookClass = Literal["gate", "context", "observe"]

CLASSES: Final[Mapping[HookName, HookClass]] = {
    "session_start": "context",
    "session_end": "observe",
    "before_input": "gate",
    "before_model": "gate",
    "after_model": "gate",
    "before_tool": "gate",
    "permission_request": "gate",
    "permission_denied": "observe",
    "after_tool": "observe",
    "before_tool_result": "gate",
    "after_tool_batch": "context",
    "before_compact": "gate",
    "after_compact": "context",
    "on_stop": "gate",
    "on_stop_failure": "observe",
    "subagent_start": "gate",
    "subagent_stop": "gate",
    "before_model_switch": "gate",
    "after_model_switch": "observe",
    "notification": "observe",
}
"""gate: a failure denies the work. context: returns injections the step waits for, and a
failure denies that step. observe: can't change execution; a failure is recorded and ignored."""


def wire_name(hook: HookName) -> str:
    return "stop_failure" if hook == "on_stop_failure" else hook


class Proceed(TypedDict):
    decision: Literal["proceed"]
    injections: NotRequired[Sequence[str]]


class Allow(TypedDict):
    decision: Literal["allow"]
    injections: NotRequired[Sequence[str]]


class Deny(TypedDict):
    decision: Literal["deny"]
    reason: str


class Ask(TypedDict):
    decision: Literal["ask"]
    rule: NotRequired[str]


class Guide(TypedDict):
    decision: Literal["guide"]
    text: str


class Retry(TypedDict):
    decision: Literal["retry"]
    reason: str


class Stop(TypedDict):
    decision: Literal["stop"]


class Continue(TypedDict):
    decision: Literal["continue"]
    reason: str


class Redact(TypedDict):
    decision: Literal["redact"]
    spans: Sequence[Span]


type InputDecision = Allow | Deny
type ModelGate = Proceed | Deny
type ResponseGate = Proceed | Deny | Guide | Retry
type ToolGate = Allow | Deny | Ask
type ResultGate = Proceed | Redact | Deny
type CompactGate = Proceed | Deny | Guide
type StopGate = Stop | Continue
type SwitchGate = Allow | Deny

type Fn[*A, R] = Callable[[*A], Awaitable[R]]
type Source = Literal["startup", "resume", "fork", "compact"]


class Hooks(TypedDict, total=False):
    """spec/api.json `Hooks`: every hook point, each optional. ponytail: hooks see the run's
    context without deps (`deps` is None); typed deps for hooks when a use needs them."""

    session_start: Fn[
        Literal["startup", "resume", "fork", "compact"], RunContext[None], Sequence[str]
    ]
    session_end: Fn[RunContext[None], None]
    before_input: Fn[UserInputData, RunContext[None], InputDecision]
    before_model: Fn[ReducedState, RunContext[None], ModelGate]
    after_model: Fn[ReducedState, ModelResponseData, RunContext[None], ResponseGate]
    before_tool: Fn[ToolCallData, RunContext[None], ToolGate]
    permission_request: Fn[ToolCallData, RunContext[None], ToolGate]
    permission_denied: Fn[ToolCallData, RunContext[None], None]
    after_tool: Fn[ToolCallData, ToolResultData, RunContext[None], Sequence[str]]
    before_tool_result: Fn[ToolCallData, ToolResultData, RunContext[None], ResultGate]
    after_tool_batch: Fn[ReducedState, RunContext[None], Sequence[str]]
    before_compact: Fn[ReducedState, RunContext[None], CompactGate]
    after_compact: Fn[ReducedState, RunContext[None], Sequence[str]]
    on_stop: Fn[ReducedState, RunContext[None], StopGate]
    on_stop_failure: "Fn[RunErrorCode, RunContext[None], None]"
    subagent_start: Fn[ToolCallData, RunContext[None], SwitchGate]
    subagent_stop: Fn[AgentFinishedData, RunContext[None], StopGate]
    before_model_switch: Fn[ModelSettings, RunContext[None], SwitchGate]
    after_model_switch: Fn[ModelSettings, RunContext[None], None]
    notification: Fn[LogEvent, RunContext[None], None]
