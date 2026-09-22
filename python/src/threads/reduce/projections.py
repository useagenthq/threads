"""Named projections beyond ReducedState (spec/conformance/README.md, "Projections").

A projection whose pinned policy section is absent is `None`: there is nothing to project it
against, and the ADR defaults don't price anything.
"""

from collections.abc import Callable, Mapping
from dataclasses import dataclass

from pydantic import JsonValue
from pydantic.experimental.missing_sentinel import MISSING

from threads.log import (
    AgentFinishedEvent,
    AgentSpawnedEvent,
    CompactedEvent,
    CompactionFailedEvent,
    ContextEditedEvent,
    EventId,
    Model,
    ModelAttemptAbandonedData,
    ModelAttemptAbandonedEvent,
    ModelRequestEvent,
    ModelResponseEvent,
    ModelResponseRecoveredEvent,
    ModelSettings,
    SettingsChangedEvent,
    TodosUpdatedEvent,
    ToolsChangedEvent,
    Usage,
)
from threads.reduce.fold import Fold, policy
from threads.reduce.handlers import to_json

_DROP_MIN = 2000
_CAUSES = (SettingsChangedEvent, CompactedEvent, ContextEditedEvent, ToolsChangedEvent)


@dataclass(frozen=True, slots=True)
class _Attempt:
    request: ModelRequestEvent
    model: Model | None
    max_tokens: JsonValue
    usage: Usage | None = None
    abandoned: ModelAttemptAbandonedData | None = None


def _prices(model: Model) -> Mapping[str, int]:
    price = model.price
    if price is MISSING:
        return {}
    prices = {"input": price.input, "output": price.output}
    if price.cache_read is not MISSING:
        prices["cache_read"] = price.cache_read
    if price.cache_write is not MISSING:
        prices["cache_write"] = price.cache_write
    return prices


def _bound(attempt: _Attempt) -> int | None:
    return bound(attempt.model, attempt.max_tokens, attempt.request.data.input_bound_tokens)


def bound(model: Model | None, max_tokens: JsonValue, input_tokens: int | MISSING) -> int | None:
    """An attempt's reservation: its input bound at the highest input-side
    price plus max_tokens at the output price. None when the model declares no bound."""
    if model is None or not isinstance(max_tokens, int):
        return None
    if input_tokens is MISSING:
        if model.input_billing_bound != "context_window":
            return None
        input_tokens = model.context_window
    prices = _prices(model)
    top = max(prices.get(k, 0) for k in ("input", "cache_read", "cache_write"))
    return input_tokens * top + max_tokens * prices.get("output", 0)


def _response_cost(usage: Usage, model: Model | None, bound: int | None) -> tuple[int, int | None]:
    """(known nanos, upper nanos or None when unbounded) for one response."""
    prices: Mapping[str, int] = _prices(model) if model is not None else {}
    fields: Mapping[str, int | MISSING | None] = {
        "input": usage.input_tokens,
        "output": usage.output_tokens,
        "cache_read": usage.cache_read_tokens,
        "cache_write": usage.cache_write_tokens,
    }
    present: Mapping[str, int | None] = {k: v for k, v in fields.items() if v is not MISSING}
    known = sum(v * prices.get(k, 0) for k, v in present.items() if v is not None)
    if all(v is not None for v in present.values()):
        return known, known
    return known, None if bound is None else known + bound


def _attempts(fold: Fold, models: Mapping[tuple[str, str], Model]) -> list[_Attempt]:
    started = fold.started
    if started is None:
        return []
    settings: ModelSettings | None = None
    attempts: dict[EventId, _Attempt] = {}
    for event in fold.events:
        if isinstance(event, SettingsChangedEvent):
            settings = event.data.settings
        elif isinstance(event, ModelRequestEvent):
            ref = started.model if settings is None else settings.model
            params = started.model_params if settings is None else settings.model_params
            model = models.get((ref.provider, ref.name))
            attempts[event.event_id] = _Attempt(event, model, params.get("max_tokens"))
        elif isinstance(event, ModelResponseEvent | ModelResponseRecoveredEvent):
            _settle(attempts, event.data.request_event_id, usage=event.data.usage)
        elif isinstance(event, ModelAttemptAbandonedEvent):
            _settle(attempts, event.data.request_event_id, abandoned=event.data)
    return list(attempts.values())


def _settle(
    attempts: dict[EventId, _Attempt],
    request: EventId,
    usage: Usage | None = None,
    abandoned: ModelAttemptAbandonedData | None = None,
) -> None:
    attempt = attempts.get(request)
    if attempt is not None:
        attempts[request] = _Attempt(
            attempt.request,
            attempt.model,
            attempt.max_tokens,
            usage or attempt.usage,
            abandoned or attempt.abandoned,
        )


def _not_billed(abandoned: ModelAttemptAbandonedData | None) -> bool:
    if abandoned is None:
        return False
    return abandoned.provider_outcome == "not_sent" or abandoned.billing == "not_billed"


def dispositions(fold: Fold) -> list[tuple[int, int, int | None]]:
    """One disposition per billable model_request: its seq, its known nanos,
    and its upper bound (None when unbounded). An attempt proven not billed has none."""
    pinned = policy(fold)
    if pinned is None or pinned.models is MISSING:
        return []
    models = {(m.provider, m.name): m for m in pinned.models}
    out: list[tuple[int, int, int | None]] = []
    for attempt in _attempts(fold, models):
        if attempt.usage is None and _not_billed(attempt.abandoned):
            continue
        cap = _bound(attempt)
        k, u = (
            (0, cap) if attempt.usage is None else _response_cost(attempt.usage, attempt.model, cap)
        )
        out.append((attempt.request.seq, k, u))
    return out


def cost(fold: Fold) -> JsonValue:
    pinned = policy(fold)
    if pinned is None or pinned.models is MISSING or pinned.currency is MISSING:
        return None
    known = upper = 0
    complete = bounded = True
    for _, k, u in dispositions(fold):
        known += k
        complete = complete and u == k
        bounded = bounded and u is not None
        upper += k if u is None else u
    return {
        "currency": pinned.currency,
        "known_nanos": known,
        "upper_bound_nanos": upper,
        "complete": complete,
        "bounded": bounded,
    }


def cache_breaks(fold: Fold) -> JsonValue:
    """Cache reads falling below 95% of the previous turn response's by at least 2000."""
    pinned = policy(fold)
    if pinned is None or pinned.context is MISSING:
        return None
    ttl = pinned.context.cache_ttl_ms
    requests = {e.event_id: e for e in fold.events if isinstance(e, ModelRequestEvent)}
    breaks: list[JsonValue] = []
    previous: tuple[int, int] | None = None  # (cache reads, response time)
    seen: list[str] = []
    for event in fold.events:
        if isinstance(event, _CAUSES):
            seen.append(event.type)
            continue
        if not isinstance(event, ModelResponseEvent):
            continue
        request = requests.get(event.data.request_event_id)
        reads = event.data.usage.cache_read_tokens
        if request is None or request.data.purpose == "compaction" or not isinstance(reads, int):
            continue
        if previous is not None and _dropped(previous[0], reads):
            gap = request.time - previous[1]
            cause = seen[0] if seen else ("ttl_expired" if gap > ttl else "unknown")
            breaks.append({"request_event_id": request.event_id, "likely_cause": cause})
        previous, seen = (reads, event.time), []
    return breaks


def _dropped(previous: int, reads: int) -> bool:
    return 20 * reads < 19 * previous and previous - reads >= _DROP_MIN


def compaction(fold: Fold) -> JsonValue:
    """The derived circuit breaker."""
    pinned = policy(fold)
    if pinned is None or pinned.context is MISSING:
        return None
    failures = 0
    for event in fold.events:
        if isinstance(event, CompactedEvent):
            failures = 0
        elif isinstance(event, CompactionFailedEvent):
            failures += 1
    limit = pinned.context.compact.max_failures
    return {"consecutive_failures": failures, "breaker_open": failures >= limit}


def todos(fold: Fold) -> JsonValue:
    latest = [e for e in fold.events if isinstance(e, TodosUpdatedEvent)]
    return [to_json(todo) for todo in latest[-1].data.todos] if latest else []


def children(fold: Fold) -> JsonValue:
    status: dict[str, str] = {}
    for event in fold.events:
        if isinstance(event, AgentSpawnedEvent):
            status[event.data.child_thread_id] = "running"
        elif isinstance(event, AgentFinishedEvent):
            status[event.data.child_thread_id] = event.data.status
    return [{"child_thread_id": child, "status": s} for child, s in status.items()]


def team_tasks(fold: Fold) -> JsonValue:
    tasks: list[JsonValue] = []
    for task_id, task in fold.tasks.items():
        entry: dict[str, JsonValue] = {"task_id": task_id, "status": task.status}
        if task.owner is not None:
            entry["owner"] = task.owner
        tasks.append(entry)
    return tasks


def mode(fold: Fold) -> JsonValue:
    return fold.mode


PROJECTIONS: Mapping[str, Callable[[Fold], JsonValue]] = {
    "cost": cost,
    "cache_breaks": cache_breaks,
    "compaction": compaction,
    "todos": todos,
    "children": children,
    "team_tasks": team_tasks,
    "mode": mode,
}
"""By name. `model` and `output` aren't implemented yet; no case lists them."""
