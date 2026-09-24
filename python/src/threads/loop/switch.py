"""The settings changes the loop makes itself (ADR 0020): a fallback after repeated overloads,
and, with `fallback_scope: turn`, the revert to the settings before the fallback at the next
input. before_model_switch gates both; a deny keeps the current epoch."""

from threads.hooks.runner import SWITCH, decision_draft
from threads.log import (
    Event,
    HookDecisionEvent,
    ModelSettings,
    SettingsChangedEvent,
    UserInputEvent,
)
from threads.loop import epoch, gates
from threads.loop.defaults import fallbacks, retry
from threads.loop.drafts import draft
from threads.loop.gates import said, verdict
from threads.loop.runtime import Barred, Halt, Runtime, lost
from threads.reduce.fold import Fold
from threads.reduce.handlers import to_json
from threads.result import Err
from threads.store import Draft


def next_fallback(fold: Fold) -> ModelSettings | None:
    """The next `policy.fallback` entry after the fallbacks already taken since the last
    change of another kind (thread start, a user change, an escalation or a revert)."""
    taken = 0
    for event in reversed(fold.events):
        if isinstance(event, SettingsChangedEvent):
            if event.data.reason != "fallback":
                break
            taken += 1
    entries = fallbacks(fold)
    return entries[taken] if taken < len(entries) else None


async def gate(rt: Runtime, settings: ModelSettings, **ids: str) -> tuple[list[Draft], bool]:
    """Asks before_model_switch: its decisions to record, and whether every hook allowed."""
    ran = await rt.hooks.run("before_model_switch", SWITCH, settings)
    hook = "before_model_switch"
    drafts = [decision_draft(hook, r, verdict(r), said(r, "reason"), **ids) for r in ran]
    return drafts, all(verdict(r) == "allow" for r in ran)


def revert_due(fold: Fold) -> tuple[UserInputEvent, ModelSettings] | None:
    """The input that owes a revert and the settings to restore, or None. It is owed when the
    current epoch was entered by a fallback, the scope is turn, the latest input came after that
    fallback, and before_model_switch has not decided on that input yet (an allow reverted, a
    deny keeps the fallback for the input's turn). Derived from the log alone, so a crash never
    asks the hook twice for one input."""
    changes = [e for e in fold.events if isinstance(e, SettingsChangedEvent)]
    user = next((e for e in reversed(fold.events) if isinstance(e, UserInputEvent)), None)
    if user is None or not changes or changes[-1].data.reason != "fallback":
        return None
    if retry(fold).fallback_scope != "turn" or user.seq < changes[-1].seq:
        return None
    if any(_decided(e, user) for e in fold.events if e.seq > user.seq):
        return None
    before = next((c.data.settings for c in reversed(changes) if c.data.reason != "fallback"), None)
    return user, before or epoch.pinned(fold)


def _decided(event: Event, user: UserInputEvent) -> bool:
    return (
        isinstance(event, HookDecisionEvent)
        and event.data.hook == "before_model_switch"
        and event.data.input_event_id == user.event_id
    )


async def revert(rt: Runtime) -> Halt | None:
    """Reverts a turn-scoped fallback before the turn's first request, when one is owed. The
    hook's decisions and the revert (or only the deny) are one batch: a crash leaves either
    nothing, so the hook is asked again, or the whole outcome."""
    due = revert_due(rt.fold)
    if due is None:
        return None
    user, settings = due
    drafts, allowed = await gate(rt, settings, input_event_id=user.event_id)
    if allowed:
        data = {"reason": "revert", "settings": to_json(settings), "cause_event_id": user.event_id}
        drafts.append(draft("settings_changed", data))
    done = await rt.append(*drafts)
    if isinstance(done, Err):
        return lost(done.error)
    if isinstance(done, Barred):
        return None  # a cancel landed first: the revert waits for the next turn
    return await gates.observe(rt, "after_model_switch", settings) if allowed else None
