"""Thread controls (spec/api.json `Thread`: answer, resolve_parked, cancel, set_model, set_mode):
each appends one durable record under the branch's single writer, with the acting principal as
its actor. The record is the control; a run picks it up from the log.

A run in flight in this process owns the branch's lease, so a control appends through that
run's writer (`LIVE`); otherwise it takes the lease for the one append and hands it back.
"""

import uuid
from collections.abc import Callable, Sequence
from typing import TYPE_CHECKING, Final, Literal

from pydantic.experimental.missing_sentinel import MISSING

from threads._generated.host_api_v1 import Appended, SettingsChange
from threads.agents.store import LIVE, Store, now_ms, open_store
from threads.log import (
    BranchId,
    ModelSettings,
    ParkAddress,
    ParseError,
    PermissionMode,
    Principal,
    SettingsChangedEvent,
    ThreadStartedEvent,
    UserInputEvent,
)
from threads.loop.runtime import LOST
from threads.reduce import Fold
from threads.reduce.handlers import to_json
from threads.result import Err, Ok
from threads.store import Draft, StoredEvent, Writer
from threads.store.companion import Companion
from threads.store.lines import uuid7

if TYPE_CHECKING:
    from pydantic import JsonValue

LOCAL_OPERATOR: Final = Principal(issuer="api", tenant="local", subject="operator")
"""The default principal of a local run, and its default approver."""

type Controlled = Ok[Appended] | Err[ParseError]
type Build = Callable[[Fold], Ok[Sequence[Draft]] | Err[ParseError]]


def principal_key(principal: Principal) -> str:
    """The normalized issuer/tenant/subject PrincipalKey."""
    return f"{principal.issuer}/{principal.tenant}/{principal.subject}"


def actor(kind: Literal["user", "approver"], principal: Principal) -> "dict[str, JsonValue]":
    return {"kind": kind, "principal": to_json(principal)}


def forbidden(why: str) -> Err[ParseError]:
    return Err(ParseError("forbidden", why))


async def append(
    store: Store, branch: BranchId, build: Build, companion: Companion | None = None
) -> Controlled:
    """Builds the drafts from the committed branch and appends them as one transaction. The
    first draft is the control's durable record."""

    async def under(writer: Writer) -> Controlled:
        drafts = build(writer.fold)
        if isinstance(drafts, Err):
            return drafts
        done = await writer.append(drafts.value, companion)
        if isinstance(done, Err):
            if done.error.code in LOST:
                # The run here lost the branch to another holder: to the caller that is the
                # same as a lease held elsewhere.
                return Err(ParseError("branch_busy", done.error.message))
            return done
        return Ok(Appended(event_id=done.value[0].event_id))

    live = LIVE.get(branch)
    if live is not None:
        return await under(live)
    sq = await open_store(store)
    taken = await sq.acquire(branch, uuid.uuid4().hex, now_ms)
    if isinstance(taken, Err):
        return taken
    try:
        return await under(taken.value)
    finally:
        await taken.value.release()


def resumed(fold: Fold, address: ParkAddress, cause: str) -> tuple[Draft, ...]:
    """A resumed for the address when the branch is parked on it."""
    if address not in fold.parked:
        return ()
    data: dict[str, JsonValue] = {"address": to_json(address), "cause_event_id": cause}
    return (Draft("resumed", data),)


def first(kind: str, data: "dict[str, JsonValue]", by: "dict[str, JsonValue]") -> Draft:
    return Draft(kind, data, by, True, uuid7(now_ms()))


async def cancel(
    store: Store,
    branch: BranchId,
    principal: Principal,
    *,
    kind: Literal["cancel_requested", "stop_when_idle"] = "cancel_requested",
    companion: Companion | None = None,
) -> Controlled:
    """A durable cancel_requested: the run stops at its next step, and an effect in doubt is
    settled or parked first, never cancelled over. stop_when_idle (a channel's soft stop)
    finishes what is in flight and starts nothing new."""
    if principal.tenant != store.tenant:
        return forbidden("another tenant's thread")
    if kind == "stop_when_idle":
        data: dict[str, JsonValue] = {"reason": "requested by the thread's principal"}
        soft = Draft(kind, data, actor("user", principal))
        return await append(store, branch, lambda _: Ok((soft,)), companion)
    barrier = first(kind, {"scope": "thread"}, actor("user", principal))
    return await append(store, branch, lambda fold: Ok(barred(fold, barrier)), companion)


def barred(fold: Fold, barrier: Draft) -> tuple[Draft, ...]:
    """The cancel_requested barrier, then a resumed for each open question or approval: those
    can't be answered after the barrier, so the loop closes their calls. An effect in doubt
    stays parked."""
    waiting = [a for a in fold.parked if a.kind in ("approval", "input")]
    return (barrier, *(d for a in waiting for d in resumed(fold, a, _id(barrier))))


async def set_mode(
    store: Store, branch: BranchId, mode: PermissionMode, principal: Principal
) -> Controlled:
    """mode_changed from the current mode; bypass needs allow_bypass, which
    the log's own rules check (invalid_transition)."""
    if principal.tenant != store.tenant:
        return forbidden("another tenant's thread")

    def build(fold: Fold) -> Ok[Sequence[Draft]]:
        data: dict[str, JsonValue] = {"from": fold.mode, "to": mode}
        return Ok((Draft("mode_changed", data, actor("user", principal)),))

    return await append(store, branch, build)


async def set_model(
    store: Store, branch: BranchId, settings: SettingsChange, principal: Principal
) -> Controlled:
    """settings_changed{reason: user}: a new settings epoch. The adapter and,
    by default, the model params carry over from the current epoch."""
    if principal.tenant != store.tenant:
        return forbidden("another tenant's thread")

    def build(fold: Fold) -> Ok[Sequence[Draft]] | Err[ParseError]:
        current = _settings(fold.events)
        if current is None:
            return Err(ParseError("invalid_transition", "the thread has not started"))
        params = settings.model_params
        carryover = settings.reasoning_carryover
        new = ModelSettings(
            model=settings.model,
            model_params=current.model_params if params is MISSING else params,
            adapter=current.adapter,
            reasoning_carryover="keep" if carryover is MISSING else carryover,
        )
        data: dict[str, JsonValue] = {"reason": "user", "settings": to_json(new)}
        return Ok((Draft("settings_changed", data, actor("user", principal)),))

    return await append(store, branch, build)


def _settings(events: Sequence[StoredEvent]) -> ModelSettings | None:
    for event in reversed(events):
        if isinstance(event, SettingsChangedEvent):
            return event.data.settings
        if isinstance(event, ThreadStartedEvent):
            data = event.data
            return ModelSettings(
                model=data.model,
                model_params=data.model_params,
                adapter=data.adapter,
                reasoning_carryover="keep",
            )
    return None


def requester(events: Sequence[StoredEvent]) -> Principal | None:
    """The principal whose input opened the current turn."""
    for event in reversed(events):
        if isinstance(event, UserInputEvent):
            return event.actor.principal
    return None


def answer_text(text: str | Sequence[str]) -> str:
    """A list answer joins with newlines: a comma may sit inside a choice (spec, Multi-choice)."""
    return text if isinstance(text, str) else "\n".join(text)


async def answer(
    store: Store,
    branch: BranchId,
    call_id: str,
    text: str | Sequence[str],
    principal: Principal,
) -> Controlled:
    """The answer to an open ask_user question: tool_result{origin: answered} by the principal
    whose input opened the turn, then resumed."""
    if principal.tenant != store.tenant:
        return forbidden("another tenant's thread")
    preview = answer_text(text)
    address = ParkAddress(kind="input", id=call_id)

    def build(fold: Fold) -> Ok[Sequence[Draft]] | Err[ParseError]:
        if address not in fold.parked:
            return Err(ParseError("no_open_question", f"no open question {call_id}"))
        if requester(fold.events) != principal:
            return forbidden("only the principal who asked may answer")
        data: dict[str, JsonValue] = {
            "call_id": call_id,
            "is_error": False,
            "completeness": "complete",
            "preview": preview,
            "origin": "answered",
        }
        result = first("tool_result", data, actor("user", principal))
        return Ok((result, *resumed(fold, address, _id(result))))

    return await append(store, branch, build)


def _id(draft: Draft) -> str:
    if draft.event_id is None:
        raise AssertionError("a cause is appended with a preset id")
    return draft.event_id


async def resolve_parked(
    store: Store,
    branch: BranchId,
    effect_key: str,
    resolution: Literal["assume_done", "assume_not_done"],
    principal: Principal,
) -> Controlled:
    """A human settles an effect in doubt: effect_resolved{by: human}, then resumed (). The caller has checked the principal's authority (threads.thread.authority)."""
    address = ParkAddress(kind="effect", id=effect_key)

    def build(fold: Fold) -> Ok[Sequence[Draft]] | Err[ParseError]:
        if address not in fold.parked:
            return Err(ParseError("not_parked", f"no parked effect {effect_key}"))
        call = effect_key.split(":", 1)[-1]
        data: dict[str, JsonValue] = {"call_id": call, "outcome": resolution, "by": "human"}
        settled = first("effect_resolved", data, actor("approver", principal))
        return Ok((settled, *resumed(fold, address, _id(settled))))

    return await append(store, branch, build)
