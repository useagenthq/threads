"""The refusal for a continued thread whose pin changed says how to continue it when the change
is one prompt caching introduced (lane 10)."""

from pydantic import JsonValue

from threads.agents.pin_change import pin_change


def _started(settings: dict[str, JsonValue], ttl: int | None = None) -> JsonValue:
    data: dict[str, JsonValue] = {"adapter": {"settings": settings}}
    if ttl is not None:
        data["policy"] = {"context": {"cache_ttl_ms": ttl}}
    return data


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


def test_any_other_change_starts_a_new_thread() -> None:
    assert pin_change(_started({}), _started({})) == (
        "this thread was started with another config; a config change starts a new thread"
    )
