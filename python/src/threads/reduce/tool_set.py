"""Rule 17, points 1-5 (spec/schema/README.md): a new tool set never makes dispatch less safe
(invariant 3)."""

from pydantic.experimental.missing_sentinel import MISSING

from threads._tool_names import LOOP_TOOLS
from threads.log import ToolsChangedEvent, ToolSpec
from threads.log.jcs import canonicalize
from threads.reduce.fold import Fold
from threads.reduce.handlers import to_json
from threads.result import Ok


def tool_set_error(fold: Fold, event: ToolsChangedEvent) -> str | None:
    """Checks every spec against the one pinned or first added under its name."""
    cause = event.data.cause
    if cause is not MISSING and cause.kind != "tool_search":
        return f"tools_changed cause {cause.kind} has no writer"
    if cause is not MISSING and (cause.call_id is MISSING or cause.call_id not in fold.calls):
        return "a tool_search cause names no earlier tool_call"
    added: dict[str, ToolSpec] = {}
    for spec in event.data.tools:
        first = fold.known_tools.get(spec.name, added.get(spec.name))
        if first is None:
            error = _added_error(spec)
            added[spec.name] = spec
        else:
            error = _kept_error(fold, first, spec, searched=cause is not MISSING)
        if error is not None:
            return error
    return None


def _added_error(spec: ToolSpec) -> str | None:
    """Point 3: a name nothing pinned enters as unguarded, so uncertainty about it always
    parks."""
    if spec.name in LOOP_TOOLS:
        return f"tools_changed adds {spec.name}, a tool the loop runs itself with no effect record"
    if (
        spec.effect_class == "unguarded"
        and spec.dedup_window_ms is MISSING
        and spec.ends_turn is MISSING
    ):
        return None
    return (
        f"tools_changed adds {spec.name}, which is not unguarded with no dedup window or ends_turn"
    )


def _kept_error(fold: Fold, first: ToolSpec, spec: ToolSpec, *, searched: bool) -> str | None:
    """Points 1-3: the first spec holds but for defer_loading, which only goes true to absent."""
    if not _same(first, spec):
        return f"tools_changed changes the spec of {spec.name}"
    before = fold.tools.get(spec.name, first)
    was, now = before.defer_loading is True, spec.defer_loading is True
    if was == now:
        return None
    if now:
        return f"tools_changed defers {spec.name} again"
    return None if searched else f"tools_changed loads {spec.name} without a tool_search"


def _same(a: ToolSpec, b: ToolSpec) -> bool:
    x = _text(a)
    return x is not None and x == _text(b)


def _text(spec: ToolSpec) -> str | None:
    """The RFC 8785 text of a spec without defer_loading."""
    text = canonicalize(to_json(spec.model_copy(update={"defer_loading": MISSING})))
    return text.value if isinstance(text, Ok) else None
