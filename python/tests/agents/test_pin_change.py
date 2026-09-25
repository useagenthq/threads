"""The refusal for a continued thread whose pin changed says how to continue it when the change
is one prompt caching introduced (lane 10), and says a pin from before resolved settings can't be
continued."""

from pydantic import JsonValue

from threads.agents.pin_change import pin_change


def _started(
    settings: dict[str, JsonValue], ttl: int = 300_000, tools: JsonValue = None
) -> JsonValue:
    policy: JsonValue = {"permissions": {}, "retry": {}, "context": {"cache_ttl_ms": ttl}}
    started: dict[str, JsonValue] = {"adapter": {"settings": settings}, "policy": policy}
    return started if tools is None else {**started, "tools": tools}


def test_a_python_pin_without_its_resolved_settings_starts_a_new_thread() -> None:
    legacy: JsonValue = {"adapter": {"settings": {}}, "policy": {"models": []}}
    assert pin_change(legacy, _started({})) == (
        "this thread was started by an older Python release that didn't pin its default "
        "permissions, retry and context settings; its config can't be matched now, so start a "
        "new thread"
    )


def test_a_thread_from_before_prompt_caching_names_prompt_cache_false() -> None:
    assert pin_change(_started({}), _started({"prompt_cache": "5m"})) == (
        "this thread was started before prompt caching: pass prompt_cache=False to anthropic() "
        "(promptCache: false in TypeScript) to continue it"
    )


def test_a_newly_declared_cache_lifetime_names_the_value_that_continues_it() -> None:
    assert pin_change(_started({}), _started({}, 86_400_000)) == (
        "this thread judges cache breaks by a 300000 ms cache lifetime, and the agent's models "
        "declare 86400000 ms: set cache_ttl_ms in the agent's context to 300000 to continue it"
    )


def test_an_open_egress_thread_from_before_the_effect_class_rule_starts_a_new_thread() -> None:
    def pinned(write: str) -> JsonValue:
        tools: JsonValue = [
            {"name": "bash", "effect_class": "unguarded"},
            {"name": "write", "effect_class": write},
        ]
        return _started({}, tools=tools)

    assert pin_change(pinned("sandbox_local"), pinned("unguarded")) == (
        'this thread was started with open egress (egress="unenforced") by an older release, '
        "which pinned write, edit and notebook_edit as sandbox_local; under open egress they are "
        "now unguarded, so an in-doubt call parks instead of being settled by the sandbox. Its "
        "config can't be matched now, so start a new thread"
    )
    # Under deny-all nothing changed: any other change is refused as before.
    assert pin_change(pinned("sandbox_local"), pinned("sandbox_local")) == (
        "this thread was started with another config; a config change starts a new thread"
    )


def test_any_other_change_starts_a_new_thread() -> None:
    assert pin_change(_started({}), _started({})) == (
        "this thread was started with another config; a config change starts a new thread"
    )
