"""USER_EVENTS is exhaustive (spec lane 22, A.2): every event type and every injected source is
classified once, so a lane that adds an extension append API or a new injected source fails here
until it says whether the offline rerun can script it."""

from typing import Literal, get_args, get_origin

from threads.evals.kinds import USER_EVENTS
from threads.log import Actor, Event, InjectedData


def _literals(annotation: object) -> set[str]:
    """Every string a Literal annotation (or a union holding one) allows."""
    if get_origin(annotation) is Literal:
        return {str(a) for a in get_args(annotation)}
    return {v for arg in get_args(annotation) for v in _literals(arg)}


def _event_types() -> set[str]:
    union = get_args(Event.__value__)[0]
    return {
        t for model in get_args(union) for t in _literals(model.model_fields["type"].annotation)
    }


def test_every_event_type_is_scriptable_or_the_frameworks_own_never_both() -> None:
    classified = [*USER_EVENTS.scriptable.events, *USER_EVENTS.framework.events]
    assert sorted(classified) == sorted(_event_types())
    assert len(set(classified)) == len(classified)


def test_every_injected_source_is_classified_once() -> None:
    sources = [
        *USER_EVENTS.scriptable.sources,
        *USER_EVENTS.framework.sources,
        *USER_EVENTS.child_thread.sources,
    ]
    assert sorted(sources) == sorted(_literals(InjectedData.model_fields["source"].annotation))
    assert len(set(sources)) == len(sources)


def test_no_actor_kind_lets_user_code_append_events_of_its_own() -> None:
    assert "extension" not in _literals(Actor.model_fields["kind"].annotation)
    assert USER_EVENTS.scriptable.events == ("hook_decision", "injected")
