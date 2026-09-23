"""The settings epoch in force: the latest `settings_changed`, else the pinned model. Dispatch,
budgets and the context window all follow it (ADR 0020)."""

from pydantic.experimental.missing_sentinel import MISSING

from threads.log import Model, ModelSettings, SettingsChangedEvent
from threads.reduce.fold import Fold, policy


def current(fold: Fold) -> ModelSettings:
    """The current epoch's settings. The pin's own epoch carries its reasoning as recorded."""
    changed = next(
        (e.data.settings for e in reversed(fold.events) if isinstance(e, SettingsChangedEvent)),
        None,
    )
    return changed if changed is not None else pinned(fold)


def pinned(fold: Fold) -> ModelSettings:
    """The settings `thread_started` pinned: the epoch a thread starts in."""
    started = fold.started
    if started is None:
        raise AssertionError("a thread with a settings epoch has its thread_started")
    return ModelSettings(
        model=started.model,
        model_params=started.model_params,
        adapter=started.adapter,
        reasoning_carryover="keep",
    )


def limits(fold: Fold) -> Model | None:
    """The `policy.models` entry of the current epoch's model: its window, output cap and
    price. None when the policy lists no models or not this one."""
    pinned_policy = policy(fold)
    if pinned_policy is None or pinned_policy.models is MISSING:
        return None
    ref = current(fold).model
    return next(
        (m for m in pinned_policy.models if (m.provider, m.name) == (ref.provider, ref.name)),
        None,
    )
