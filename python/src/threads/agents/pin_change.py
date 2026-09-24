"""Why a continued thread's pin differs from the agent's, in words that say how to continue it.
Two pin changes came with prompt caching (lane 10), and one with tool origins (lane 22): every
thread started before them meets them on upgrade. A Python thread pinned before configs recorded
their resolved settings can't continue; any other change starts a new thread."""

from typing import Final

from pydantic import JsonValue

_DEFAULT_TTL_MS: Final = 300_000


def pin_change(stored: JsonValue, new: JsonValue) -> str:
    """The refusal for continuing a thread whose thread_started data is `stored` with an agent
    that pins `new`."""
    if _unresolved(stored):
        return (
            "this thread was started by an older Python release that didn't pin its default "
            "permissions, retry and context settings; its config can't be matched now, so start "
            "a new thread"
        )
    # Checked before the caching advice: an agent with extension tools can't continue such a
    # thread whatever its caching settings.
    if _has_origins(new) and not _has_origins(stored):
        return (
            "this thread was started with another config: tool origin was added in this release; "
            "start a new thread"
        )
    if _prompt_cache(new) is not None and _prompt_cache(stored) is None:
        return (
            "this thread was started before prompt caching: pass prompt_cache=False to anthropic() "
            "(promptCache: false in TypeScript) to continue it"
        )
    was, now = _ttl(stored), _ttl(new)
    if was != now:
        return (
            f"this thread judges cache breaks by a {was} ms cache lifetime, and the agent's models "
            f"declare {now} ms: set cache_ttl_ms in the agent's context to {was} to continue it"
        )
    return "this thread was started with another config; a config change starts a new thread"


def _unresolved(started: JsonValue) -> bool:
    """A pin missing any of permissions, retry and context, whatever wrote it: only an older
    Python release pinned one from agent()."""
    policy = started.get("policy") if isinstance(started, dict) else None
    return not isinstance(policy, dict) or any(
        k not in policy for k in ("permissions", "retry", "context")
    )


def _prompt_cache(started: JsonValue) -> JsonValue:
    adapter = started.get("adapter") if isinstance(started, dict) else None
    settings = adapter.get("settings") if isinstance(adapter, dict) else None
    return settings.get("prompt_cache") if isinstance(settings, dict) else None


def _ttl(started: JsonValue) -> JsonValue:
    policy = started.get("policy") if isinstance(started, dict) else None
    context = policy.get("context") if isinstance(policy, dict) else None
    ttl = context.get("cache_ttl_ms") if isinstance(context, dict) else None
    return _DEFAULT_TTL_MS if ttl is None else ttl


def _has_origins(started: JsonValue) -> bool:
    tools = started.get("tools") if isinstance(started, dict) else None
    return isinstance(tools, list) and any(isinstance(t, dict) and "origin" in t for t in tools)
