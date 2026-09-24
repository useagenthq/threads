# pyright: strict
"""tool_search vectors (spec/schema/README.md, "Deferred tools and tool_search"): the normative
fold and tokens, the query split, the search itself, and the provider tool list an adapter sends
(Render v1, "Provider tools"). Expected values come from the reference in ucd.py, search_ref.py
and render.py, never from an implementation."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

from .common import CASES, arr, obj, text
from .jcs import JsonValue
from .log import Log
from .pieces import answer, dump, user
from .render import render
from .search_ref import search
from .tool_search import COMMENT, CREATE, ISSUES, JIRA, compaction, pinned, searched
from .tool_sets import changed
from .ucd import VERSION, fold, tables, terms, tokens

if TYPE_CHECKING:
    import pathlib

    from .jcs import Obj

VECTORS = CASES.parent / "vectors"
FOLD_INPUTS = (
    "\u00df",
    "\u1e9e",
    "\u017f",
    "\ufb01",
    "\u0130",
    "\u03bb\u03cc\u03b3\u03bf\u03c2",
    "\u039b\u038c\u0393\u039f\u03a3",
    "\uab70\uab71",
    "\uff23\uff52\uff45\uff41\uff54\uff45\uff3f\uff29\uff53\uff53\uff55\uff45",
    "\u0915\u094d\u0937\u0924\u094d\u0930\u093f\u092f",
    "e\u0301",
    "abc\U0002ebf0def",
    "x\u31efy",
    "\u0378",
    "Stra\u00dfe",
    "\u01c5ungla",
    "\u216b \u2460",
    "MCP__JIRA__Create_Issue",
    "mcp__jira__create_issue",
    "tab\there,comma",
)
SPLIT_EXTRA = (
    "a\uff0cb",
    "a\ufe50b",
    "a\ufe10b",
    "mcp__a__x\u001fmcp__a__y",
    "\ufeffmcp__a__x",
    "  a,,b  ",
    ", a ,",
    " ",
    ",",
    " , ",
    "\u3000",
)


def _fold_vector() -> Obj:
    cases: list[JsonValue] = [
        {"input": s, "fold": fold(s), "tokens": list(tokens(s))} for s in FOLD_INPUTS
    ]
    return {
        "description": (
            "The normative tool_search fold: unassigned (Unicode 15.0.0) and surrogate code "
            "points become U+0020, NFKC, full case folding from CaseFolding.txt 15.0.0 (status C "
            "and F), NFKC. tokens are the maximal runs of General_Category L, M or N code points "
            "of the fold, in order, duplicates kept."
        ),
        "unicode_version": VERSION,
        "cases": cases,
    }


def _split_vector() -> Obj:
    queries = [f"a{chr(c)}b" for c in sorted(tables().separators)] + list(SPLIT_EXTRA)
    cases: list[JsonValue] = [{"query": q, "terms": list(terms(q))} for q in queries]
    return {
        "description": (
            "The exact-name split of a tool_search query: unassigned and surrogate code points "
            "become U+0020, NFKC, then split on the pinned separators (',', every White_Space "
            "code point, U+001C-U+001F, U+FEFF), empty terms dropped. No term means no match."
        ),
        "cases": cases,
    }


def _tool(name: str, desc: str) -> tuple[str, str]:
    return name, desc


def _named(n: int) -> list[tuple[str, str]]:
    return [_tool(f"mcp__bulk__tool_{i:02d}", f"Bulk tool number {i}.") for i in range(n)]


def _jira() -> list[tuple[str, str]]:
    return [(text(t["name"]), text(t["description"])) for t in JIRA]


SEARCHES: tuple[tuple[str, int, list[tuple[str, str]], list[str]], ...] = (
    ("mcp__jira__create_issue, mcp__jira__add_comment", 5, _jira(), ["read_file"]),
    ("mcp__jira__add_comment mcp__jira__add_comment", 5, _jira(), ["read_file"]),
    ("MCP__JIRA__ADD_COMMENT\uff0cmcp__jira__search_issues", 5, _jira(), ["read_file"]),
    (", ".join(n for n, _ in _named(12)), 3, _named(12), []),
    ("mcp__jira__create_issue, comment", 2, _jira(), ["read_file"]),
    ("jira issue", 5, _jira(), ["read_file"]),
    ("jira jira issue", 1, _jira(), ["read_file"]),
    ("create", 5, _jira(), ["read_file"]),
    ("read_file, mcp__jira__create_issue", 5, _jira(), ["read_file"]),
    ("read_file", 5, _jira(), ["read_file"]),
    ("weather forecast", 5, _jira(), ["read_file"]),
    (" , ", 5, _jira(), ["read_file"]),
    ("returns key", 5, _jira(), ["read_file"]),
    ("jira", 5, [], ["read_file"]),
)


def _search_vector() -> Obj:
    cases: list[JsonValue] = []
    for query, limit, deferred, loaded in SEARCHES:
        found = search(query, limit, deferred, loaded)
        cases.append(
            {
                "query": query,
                "limit": limit,
                "deferred": [{"name": n, "description": d} for n, d in deferred],
                "loaded": list[JsonValue](loaded),
                "lines": list[JsonValue](found.lines),
                "loads": list[JsonValue](found.loaded),
            }
        )
    return {
        "description": (
            "tool_search results. deferred: the still-deferred tools in tool-set order; loaded: "
            "every other current tool. lines: the result text's lines; loads: what tools_loaded "
            "names, in order (empty: no event). Exact names first (every term a current tool's "
            "name, at most 10 loads), else keyword ranking (distinct query tokens found, score "
            "descending, then name), else 'no deferred tool matches'."
        ),
        "cases": cases,
    }


def provider_tools(body: bytes) -> list[JsonValue]:
    """The tools an adapter sends: the latest complete tool set line without its stubs, then
    every spec of the tools_loaded lines after it."""
    lines = [obj(json.loads(x)) for x in body.splitlines()]
    last = max(i for i, x in enumerate(lines) if i == 0 or x.get("role") == "tools")
    tools = [t for t in arr(lines[last]["tools"]) if "deferred" not in obj(t)]
    for x in lines[last + 1 :]:
        if x.get("role") == "tools_loaded":
            tools.extend(arr(x["tools"]))
    return tools


def _provider_logs() -> list[tuple[str, Log]]:
    stubs = Log()
    pinned(stubs, JIRA)
    user(stubs, "File a bug.")
    loads = stubs.copy()
    searched(loads, "mcp__jira__create_issue", "call_1")
    answer(loads, "Loaded.")
    user(loads, "And comments.")
    searched(loads, "mcp__jira__add_comment", "call_2")
    restated = loads.copy()
    full = {text(t["name"]): t for t in (CREATE, COMMENT, ISSUES)}
    ts = obj(next(e for e in restated.events if e["type"] == "thread_started")["data"])
    loaded = {"mcp__jira__create_issue", "mcp__jira__add_comment"}
    changed(
        restated,
        [full[text(obj(t)["name"])] if obj(t)["name"] in loaded else t for t in arr(ts["tools"])],
    )
    searched(restated, "mcp__jira__search_issues", "call_3")
    return [
        ("stubs only: the deferred tools are not offered", stubs),
        ("two loads: each tools_loaded line adds its specs", loads),
        ("a complete tools_changed after the loads, then one more load", restated),
        ("after a compaction: one merged tools_loaded line", compaction()[2]),
    ]


def _provider_vector() -> Obj:
    cases: list[JsonValue] = []
    for name, log in _provider_logs():
        body, _ = render(log.events, log.artifacts)
        cases.append(
            {
                "name": name,
                "request": [json.loads(x) for x in body.splitlines()],
                "tools": provider_tools(body),
            }
        )
    return {
        "description": (
            "The provider tool list an adapter sends for a Render v1 request: the latest complete "
            "tool set line (the last tools line, else line 0) without its stubs, then every spec "
            "of the tools_loaded lines after it, in order. A stub is never offered."
        ),
        "cases": cases,
    }


FILES = {
    "unicode-fold.json": _fold_vector,
    "query-split.json": _split_vector,
    "tool-search.json": _search_vector,
    "provider-tools.json": _provider_vector,
}


def write() -> None:
    for name, make in FILES.items():
        (VECTORS / name).write_text(dump(make()), encoding="utf-8")


def check() -> list[str]:
    out: list[str] = []
    for name, make in FILES.items():
        path: pathlib.Path = VECTORS / name
        current = path.read_text(encoding="utf-8") if path.exists() else ""
        if dump(make()) != current:
            out.append(f"{name}: differs; run gen_fixtures.py")
    return out
