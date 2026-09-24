"""Semantic rule 46 (spec/schema/README.md): a tools_loaded directly follows a successful
tool_search result for its call, and loads only reference-form tools not yet loaded, each by the
spec_ref it was pinned with. The artifact checks need artifacts, so import makes them
(render/loaded.py)."""

from collections.abc import Mapping

from pydantic.experimental.missing_sentinel import MISSING

from threads._tool_names import SEARCH
from threads.log import ParseError, ToolResultEvent, ToolsLoadedEvent
from threads.reduce.fold import Fold, reject
from threads.reduce.handlers import Handler, on


def _tools_loaded(fold: Fold, event: ToolsLoadedEvent) -> ParseError | None:
    error = _adjacent_error(fold, event) or _names_error(fold, event)
    if error is not None:
        return reject(event, error)
    for tool in event.data.tools:
        fold.loaded[tool.name] = tool.spec_ref
    return None


def _adjacent_error(fold: Fold, event: ToolsLoadedEvent) -> str | None:
    previous = fold.events[-1] if fold.events else None
    call_id = event.data.call_id
    call = fold.calls.get(call_id)
    follows = (
        isinstance(previous, ToolResultEvent)
        and previous.seq == fold.seq
        and previous.data.call_id == call_id
    )
    if not follows or call is None or call.data.name != SEARCH:
        return f"tools_loaded must directly follow the result of tool_search call {call_id}"
    if isinstance(previous, ToolResultEvent) and previous.data.is_error:
        return "tools_loaded after an error result: the search loaded nothing"
    return None


def _names_error(fold: Fold, event: ToolsLoadedEvent) -> str | None:
    names = [t.name for t in event.data.tools]
    if len(set(names)) != len(names):
        return "tools_loaded names a tool twice"
    for tool in event.data.tools:
        current = fold.tools.get(tool.name)
        pinned = fold.known_tools.get(tool.name)
        if current is None or pinned is None or current.spec_ref is MISSING:
            return f"tools_loaded names {tool.name}, which is not deferred in reference form"
        if tool.name in fold.loaded:
            return f"tools_loaded names {tool.name}, which is already loaded"
        if pinned.spec_ref != tool.spec_ref:
            return f"tools_loaded names {tool.name} with another spec_ref than its pin's"
    return None


HANDLERS: Mapping[type, Handler] = dict([on(ToolsLoadedEvent, _tools_loaded)])
