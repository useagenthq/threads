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


def _first(kind: str, data: "dict[str, JsonValue]", by: "dict[str, JsonValue]") -> Draft:
    return Draft(kind, data, by, True, uuid7(now_ms()))


async def cancel(store: Store, branch: BranchId, principal: Principal) -> Controlled:
    """A durable cancel_requested: the run stops at its next step, and an effect in doubt is
    settled or parked first, never cancelled over."""
    if principal.tenant != store.tenant:
        return forbidden("another tenant's thread")
    data: dict[str, JsonValue] = {"scope": "turn", "reason": "cancelled through the API"}
    draft = Draft("cancel_requested", data, actor("user", principal))
    return await append(store, branch, lambda _: Ok((draft,)))


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
    preview = text if isinstance(text, str) else ", ".join(text)
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
        result = _first("tool_result", data, actor("user", principal))
        return Ok((result, *resumed(fold, address, _id(result))))

    return await append(store, branch, build)


def _id(draft: Draft) -> str:
    if draft.event_id is None:
        raise AssertionError("a cause is appended with a preset id")
    return draft.event_id


async def resolve_parked(  # noqa: PLR0913 - the control, plus who may take it
    store: Store,
    branch: BranchId,
    effect_key: str,
    resolution: Literal["assume_done", "assume_not_done"],
    principal: Principal,
    *,
    approvers: Sequence[Principal],
) -> Controlled:
    """A human settles an effect in doubt: effect_resolved{by: human}, then resumed (the same approver check as an approval)."""
    if principal.tenant != store.tenant or principal not in approvers:
        return forbidden("not an approver of this thread")
    address = ParkAddress(kind="effect", id=effect_key)

    def build(fold: Fold) -> Ok[Sequence[Draft]] | Err[ParseError]:
        if address not in fold.parked:
            return Err(ParseError("not_parked", f"no parked effect {effect_key}"))
        call = effect_key.split(":", 1)[-1]
        data: dict[str, JsonValue] = {"call_id": call, "outcome": resolution, "by": "human"}
        settled = _first("effect_resolved", data, actor("approver", principal))
        return Ok((settled, *resumed(fold, address, _id(settled))))

    return await append(store, branch, build)
