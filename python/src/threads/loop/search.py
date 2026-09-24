"""The tool_search framework tool (spec/schema/README.md, "Deferred tools and tool_search"): it
matches the log's current tools and records a load as tools_loaded, in the result's batch. It is
read_only and depends only on the log and the pinned tables, so a crash before the batch runs it
again to the same bytes."""

from typing import TYPE_CHECKING, Final

from pydantic import ValidationError
from pydantic.experimental.missing_sentinel import MISSING

from threads._generated.tools_v1 import ToolSearchInput
from threads.log import JsonObject, ToolSpec
from threads.log.jcs import canonicalize
from threads.loop.drafts import draft
from threads.loop.history import CallState
from threads.loop.results import As, result_draft
from threads.loop.runtime import Halt, Runtime, lost
from threads.reduce import Fold
from threads.reduce.fold import still_deferred
from threads.reduce.handlers import to_json
from threads.result import Err, Ok
from threads.tools.tool_search import MAX_QUERY, search

if TYPE_CHECKING:
    from pydantic import JsonValue

NAME: Final = "tool_search"


def is_search(fold: Fold, spec: ToolSpec) -> bool:
    """The framework tool_search: pinned exactly when something is deferred (setup refuses a user
    tool with its name then)."""
    return spec.name == NAME and any(s.defer_loading is True for s in fold.known_tools.values())


def not_loaded(name: str) -> str:
    """The one pre-effect text for a call to a tool that is still deferred."""
    return f"tool_not_loaded: {name}; find it with tool_search first"


def invalid(input: JsonObject) -> str | None:
    """The catalog schema, then the query's length in code points."""
    text = canonicalize(dict(input))
    if not isinstance(text, Ok):
        return "invalid input: the arguments are not canonical JSON"
    try:
        parsed = ToolSearchInput.model_validate_json(text.value, strict=True)
    except ValidationError as error:
        return f"invalid input: {error.error_count()} error(s): {error}"
    if len(parsed.query) > MAX_QUERY:
        return f"invalid input: query is longer than {MAX_QUERY} characters"
    return None


async def run(rt: Runtime, state: CallState) -> Halt | None:
    call_id = state.call.data.call_id
    args = ToolSearchInput.model_validate(dict(state.call.data.input))
    fold = rt.fold
    refs = {
        s.name: s.spec_ref
        for s in fold.tools.values()
        if s.spec_ref is not MISSING and still_deferred(fold, s)
    }
    deferred = [(s.name, s.description) for s in fold.tools.values() if s.name in refs]
    others = [n for n in fold.tools if n not in refs]
    found = search(args.query, args.limit, deferred, others)
    drafts = [await result_draft(rt, call_id, "\n".join(found.lines), As("executed"))]
    if found.loaded:
        # Each by the spec_ref it was pinned with: rule 17 keeps the current one equal to it.
        tools: list[JsonValue] = [{"name": n, "spec_ref": to_json(refs[n])} for n in found.loaded]
        drafts.append(draft("tools_loaded", {"call_id": call_id, "tools": tools}))
    done = await rt.append(*drafts)
    return lost(done.error) if isinstance(done, Err) else None
