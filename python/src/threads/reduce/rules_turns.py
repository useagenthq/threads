"""Turns, model attempts, settings and thread-level rules (semantic rules 12, 17, 18, 20, 21,
26, 27 in spec/schema/README.md)."""

from collections.abc import Mapping

from pydantic import JsonValue
from pydantic.experimental.missing_sentinel import MISSING

from threads._json_schema import holds
from threads.log import (
    BudgetExceededEvent,
    HandoffEvent,
    ModeChangedEvent,
    ModelAttemptAbandonedEvent,
    ModelRequestEvent,
    ModelResponseEvent,
    ModelResponseRecoveredEvent,
    OutputValidatedEvent,
    ParseError,
    Policy,
    SettingsChangedEvent,
    SteerEvent,
    ThreadStartedEvent,
    ToolsChangedEvent,
    TurnCompletedEvent,
    UserInputEvent,
)
from threads.log.digest import canonical_sha256
from threads.log.jcs import MAX_SAFE_INTEGER
from threads.reduce.fold import Fold, policy, reject
from threads.reduce.handlers import Handler, on, to_json
from threads.result import Ok


def _thread_started(fold: Fold, event: ThreadStartedEvent) -> None:
    fold.started = event.data
    fold.tools = {spec.name: spec for spec in event.data.tools}
    pinned = policy(fold)
    if pinned is not None and pinned.permissions is not MISSING:
        fold.mode = pinned.permissions.mode


def _tools_changed(fold: Fold, event: ToolsChangedEvent) -> ParseError | None:
    # Canonical domain: the hash covers the RFC 8785 bytes of the value, so
    # re-serializing the parsed tools is the definition, not a shortcut.
    tools: JsonValue = [to_json(spec) for spec in event.data.tools]
    if canonical_sha256(tools) != Ok(event.data.tools_hash):
        return reject(event, "tools_hash is not the hash of the RFC 8785 bytes of tools")
    fold.tools = {spec.name: spec for spec in event.data.tools}
    return None


def _user_input(fold: Fold, event: UserInputEvent) -> ParseError | None:
    if fold.handed_off:
        return reject(event, "the thread handed off; it takes no more input")
    if fold.in_turn:
        return reject(event, "user_input while a turn is open; input during a turn is steer")
    fold.in_turn = True
    fold.over_budget = False
    return None


def _steer(fold: Fold, event: SteerEvent) -> ParseError | None:
    if fold.handed_off:
        return reject(event, "the thread handed off; it takes no more input")
    return None


def _model_request(fold: Fold, event: ModelRequestEvent) -> ParseError | None:
    if fold.handed_off:
        return reject(event, "the thread handed off; it makes no more model requests")
    if fold.over_budget:
        return reject(event, "model_request after budget_exceeded, before a new user_input")
    fold.open_requests.add(event.event_id)
    if event.data.purpose == "compaction":
        fold.compaction_requests.add(event.event_id)
    return None


def _model_response(fold: Fold, event: ModelResponseEvent | ModelResponseRecoveredEvent) -> None:
    data = event.data
    fold.open_requests.discard(data.request_event_id)
    fold.responses[data.request_event_id] = data
    usage = data.usage
    input_total = fold.input_tokens + (usage.input_tokens or 0)
    output_total = fold.output_tokens + (usage.output_tokens or 0)
    if input_total > MAX_SAFE_INTEGER or output_total > MAX_SAFE_INTEGER:
        # A total past the wire's integers can't be carried: the response counts as unknown.
        fold.unknown_responses += 1
        return
    fold.input_tokens, fold.output_tokens = input_total, output_total
    fold.unknown_responses += usage.input_tokens is None or usage.output_tokens is None


def _abandoned(fold: Fold, event: ModelAttemptAbandonedEvent) -> None:
    fold.open_requests.discard(event.data.request_event_id)


def _turn_completed(fold: Fold, _event: TurnCompletedEvent) -> None:
    fold.in_turn = False
    fold.turns += 1


def _settings_changed(fold: Fold, event: SettingsChangedEvent) -> ParseError | None:
    if fold.open_requests:
        return reject(event, "settings_changed while a model attempt awaits its response")
    pinned = policy(fold)
    if pinned is not None and pinned.models is not MISSING:
        model = event.data.settings.model
        listed = {(m.provider, m.name) for m in pinned.models}
        if (model.provider, model.name) not in listed:
            return reject(event, f"model {model.name} is not listed in policy.models")
    return None


def _budget_exceeded(fold: Fold, _event: BudgetExceededEvent) -> None:
    fold.over_budget = True


def _handoff(fold: Fold, _event: HandoffEvent) -> None:
    fold.handed_off = True


def _mode_changed(fold: Fold, event: ModeChangedEvent) -> ParseError | None:
    if event.data.from_ != fold.mode:
        return reject(event, f"mode_changed.from is {event.data.from_}, the mode is {fold.mode}")
    if event.data.to == "bypass" and not _allows_bypass(policy(fold)):
        return reject(event, "mode_changed to bypass needs policy.permissions.allow_bypass")
    fold.mode = event.data.to
    return None


def _allows_bypass(pinned: Policy | None) -> bool:
    if pinned is None or pinned.permissions is MISSING:
        return False
    return pinned.permissions.allow_bypass


def _output_validated(fold: Fold, event: OutputValidatedEvent) -> ParseError | None:
    pinned = policy(fold)
    if pinned is None or pinned.output is MISSING:
        return reject(event, "output_validated without policy.output")
    data = event.data
    if data.schema_sha256 != pinned.output.schema_sha256:
        return reject(event, "schema_sha256 differs from policy.output.schema_sha256")
    if data.outcome == "accepted" and not _conforms(pinned.output.schema_, data.value):
        return reject(event, "the accepted value fails policy.output.schema")
    return None


def _conforms(schema: Mapping[str, JsonValue], value: JsonValue | MISSING) -> bool:
    if value is MISSING:
        return False
    try:
        return holds(dict(schema), value)
    except TypeError:
        # A keyword this reader can't check: fail closed rather than accept unchecked output.
        return False


HANDLERS: Mapping[type, Handler] = dict(
    [
        on(ThreadStartedEvent, _thread_started),
        on(ToolsChangedEvent, _tools_changed),
        on(UserInputEvent, _user_input),
        on(SteerEvent, _steer),
        on(ModelRequestEvent, _model_request),
        on(ModelResponseEvent, _model_response),
        on(ModelResponseRecoveredEvent, _model_response),
        on(ModelAttemptAbandonedEvent, _abandoned),
        on(TurnCompletedEvent, _turn_completed),
        on(SettingsChangedEvent, _settings_changed),
        on(BudgetExceededEvent, _budget_exceeded),
        on(HandoffEvent, _handoff),
        on(ModeChangedEvent, _mode_changed),
        on(OutputValidatedEvent, _output_validated),
    ]
)
