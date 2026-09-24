# pyright: strict
"""Deferred tools and tool_search (spec/schema/README.md, "Deferred tools and tool_search"):
the reference-form pin, tools_loaded, and what the next request renders."""

from __future__ import annotations

from typing import TYPE_CHECKING

from .common import arr, obj, text, tokens, tool
from .jcs import JsonValue, Obj, canonical
from .log import Log
from .memory import catalog_spec
from .pieces import READ_FILE, REFUND, answer, call, render_case, result, started, user
from .search_ref import search

if TYPE_CHECKING:
    import pathlib

FAM = "tools_streaming"
SEARCH_BASE = catalog_spec("tool_search", "read_only")
LISTED = "\n\nDeferred tools (search to load): "
CREATE = tool(
    "mcp__jira__create_issue",
    "Create a Jira issue in a project.\nReturns the new issue key.",
    {"project": {"type": "string"}, "summary": {"type": "string"}},
    "unguarded",
)
COMMENT = tool(
    "mcp__jira__add_comment",
    "Add a comment to a Jira issue.",
    {"issue": {"type": "string"}, "body": {"type": "string"}},
    "unguarded",
)
ISSUES = tool(
    "mcp__jira__search_issues",
    "Search Jira issues with JQL.",
    {"jql": {"type": "string"}},
    "read_only",
)
JIRA = [COMMENT, CREATE, ISSUES]
SPEC_FIELDS = ("name", "description", "effect_class", "dedup_window_ms", "ends_turn")


def search_spec(deferred: list[str]) -> Obj:
    """tool_search as pinned: the catalog description, then the deferred names by code point."""
    listed = text(SEARCH_BASE["description"]) + LISTED + ", ".join(sorted(deferred))
    return {**SEARCH_BASE, "description": listed}


def full_bytes(spec: Obj) -> bytes:
    """A deferred tool's spec artifact: its complete spec without defer_loading and spec_ref."""
    return canonical({k: v for k, v in spec.items() if k not in ("defer_loading", "spec_ref")})


def ref_form(log: Log, spec: Obj) -> Obj:
    """The pinned reference form; its spec artifact is stored before thread_started."""
    ref = log.art(full_bytes(spec), "application/json")
    stub: Obj = {k: spec[k] for k in SPEC_FIELDS if k in spec}
    return {**stub, "defer_loading": True, "spec_ref": ref}


def pinned(
    log: Log, deferred: list[Obj], plain: list[JsonValue] | None = None, policy: Obj | None = None
) -> list[Obj]:
    """thread_started with tool_search (a built-in, first), the plain tools, then the deferred
    tools in reference form. Returns the stubs."""
    stubs = [ref_form(log, d) for d in deferred]
    tools: list[JsonValue] = [
        search_spec([text(d["name"]) for d in deferred]),
        *(plain if plain is not None else [READ_FILE]),
        *stubs,
    ]
    started(log, tools, policy=policy)
    return stubs


def current(log: Log) -> tuple[list[tuple[str, str]], list[str]]:
    """(the still-deferred tools as (name, description), every other current tool's name)."""
    tools: list[JsonValue] = []
    loaded: set[str] = set()
    for e in log.events:
        d = obj(e["data"])
        if e["type"] in ("thread_started", "tools_changed"):
            tools = arr(d["tools"])
        elif e["type"] == "tools_loaded":
            loaded |= {text(obj(x)["name"]) for x in arr(d["tools"])}
    specs = [obj(t) for t in tools]
    deferred = [
        (text(s["name"]), text(s["description"]))
        for s in specs
        if s.get("defer_loading") is True and s["name"] not in loaded
    ]
    names = {n for n, _ in deferred}
    return deferred, [text(s["name"]) for s in specs if s["name"] not in names]


def pinned_ref(log: Log, name: str) -> JsonValue:
    ts = obj(next(e for e in log.events if e["type"] == "thread_started")["data"])
    return next(obj(t)["spec_ref"] for t in arr(ts["tools"]) if obj(t)["name"] == name)


def searched(log: Log, query: str, call_id: str, limit: int | None = None) -> list[str]:
    """A tool_search call, its result and (when it loads anything) its tools_loaded. Returns the
    loaded names."""
    inp: Obj = {"query": query} if limit is None else {"query": query, "limit": limit}
    call(log, "tool_search", inp, call_id)
    deferred, others = current(log)
    found = search(query, 5 if limit is None else limit, deferred, others)
    result(log, call_id, "\n".join(found.lines))
    if found.loaded:
        loads: list[JsonValue] = [{"name": n, "spec_ref": pinned_ref(log, n)} for n in found.loaded]
        log.add("tools_loaded", {"call_id": call_id, "tools": loads})
    return found.loaded


def build(root: pathlib.Path) -> None:
    for name, desc, log in [*_searches(), _loaded(), _prefix_stable(), compaction()]:
        render_case(root, (name, FAM, desc), log)


def _one_search(query: str, limit: int | None = None) -> Log:
    log = Log()
    pinned(log, JIRA)
    user(log, "File a bug about the login page.")
    searched(log, query, "call_1", limit)
    return log


def _searches() -> list[tuple[str, str, Log]]:
    again = _one_search("mcp__jira__create_issue")
    answer(again, "Loaded.")
    user(again, "Load it again.")
    searched(again, "MCP__JIRA__CREATE_ISSUE, read_file", "call_2")
    return [
        (
            "tool-search-exact-names",
            "A query of two exact names, comma-separated, loads exactly those two in query "
            "order, whatever the keyword scores: one tools_loaded names each by its pinned "
            "spec_ref, and the next request renders both full specs after the result.",
            _one_search("mcp__jira__create_issue, mcp__jira__add_comment"),
        ),
        (
            "tool-search-already-loaded",
            "A second search names a loaded tool (in capitals: names match after the normative "
            "fold) and a tool that was never deferred. Every term is a current tool's name, so "
            "the result reports both as already loaded, loads nothing and writes no tools_loaded.",
            again,
        ),
        (
            "tool-search-mixed-exact-falls-back",
            "One term is an exact name and one is not, so the query falls back to keyword "
            "ranking: distinct query tokens found in name and description, highest score first, "
            "ties by name, limit 2.",
            _one_search("mcp__jira__create_issue, comment", 2),
        ),
        (
            "tool-search-ranking-tie",
            "Keyword ranking: jira and issue match add_comment and create_issue twice each and "
            "search_issues once (issues is another token). Equal scores sort by name in "
            "code-point order.",
            _one_search("jira issue"),
        ),
        (
            "tool-search-no-match",
            "No query token matches a deferred tool: the result is the text no deferred tool "
            "matches, nothing is loaded and no tools_loaded is written.",
            _one_search("weather forecast"),
        ),
    ]


def _loaded() -> tuple[str, str, Log]:
    log = Log()
    pinned(log, [CREATE])
    user(log, "File a bug about the login page.")
    searched(log, "mcp__jira__create_issue", "call_1")
    return (
        "render-deferred-tool-loaded",
        "A deferred MCP tool is pinned in reference form (stub plus spec_ref, no schema) and "
        "shows in line 0 as {name, description, deferred: true}, with tool_search listing it. "
        "The search's result is followed by tools_loaded naming its spec_ref; the next request "
        "renders a tools_loaded line with the full spec read from the artifact. Line 0 is "
        "unchanged.",
        log,
    )


def _prefix_stable() -> tuple[str, str, Log]:
    log = Log()
    pinned(log, [*JIRA, REFUND])
    user(log, "File a bug about the login page.")
    searched(log, "mcp__jira__create_issue", "call_1")
    use: Obj = {"project": "WEB", "summary": "Login fails"}
    call(log, "mcp__jira__create_issue", use, "call_2")
    log.add("effect_begin", {"call_id": "call_2", "attempt": 1})
    out = b"created WEB-7"
    log.add("effect_commit", {"call_id": "call_2", "result_ref": log.art(out, "text/plain")})
    result(log, "call_2", out.decode())
    answer(log, "Filed WEB-7.")
    user(log, "Refund charge ch_1 too.")
    searched(log, "refund", "call_3")
    answer(log, "Loaded refund_card.")
    return (
        "tool-search-prefix-stable",
        "Two loads in two turns, each followed by a recorded request. Every declared_prefix "
        "is the one line 0 (C7): a load is a history line, never a settings change. Each "
        "recorded request's bytes are a byte prefix of the next request's, since a load only "
        "appends its call, result and tools_loaded lines.",
        log,
    )


def compaction() -> tuple[str, str, Log]:
    log = Log()
    pinned(log, JIRA)
    user(log, "File a bug about the login page.")
    first = log.events[-1]
    searched(log, "mcp__jira__create_issue", "call_1")
    answer(log, "Loaded.")
    user(log, "And comments.")
    searched(log, "mcp__jira__add_comment", "call_2")
    answer(log, "Loaded.")
    last = log.events[-1]
    summary = "The user wants a bug filed; create_issue and add_comment are loaded."
    side = log.model_request(compaction=True)
    log.model_response(side, [{"type": "text", "text": summary}], "end_turn", tokens(900, 40))
    log.add(
        "compacted",
        {
            "from_seq": first["seq"],
            "to_seq": last["seq"],
            "from_event_id": first["event_id"],
            "to_event_id": last["event_id"],
            "summary_ref": log.art(summary.encode(), "text/plain"),
            "summary_request_event_id": side["event_id"],
            "trigger": "threshold",
        },
    )
    user(log, "Now file it.")
    r = log.model_request()
    log.model_response(r, [{"type": "text", "text": "Filing."}], "end_turn", tokens(90, 3))
    log.add("turn_completed", {"reason": "end_turn"})
    return (
        "tool-search-loaded-across-compaction",
        "Two loads, then a compaction over both turns. The view renders the summary, then one "
        "tools_loaded line with every tool loaded in the range, in load order, so the loads "
        "survive. The request after the compaction keeps line 0 (C7).",
        log,
    )
