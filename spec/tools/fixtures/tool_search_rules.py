# pyright: strict
"""Rule 47 (tools_loaded) and rule 17 point 6 (a reference-form pin in a later tools_changed):
reduce cases, and the artifact checks import makes (render cases)."""

from __future__ import annotations

from typing import TYPE_CHECKING

from .common import aref, arr, obj, text
from .jcs import JsonValue, Obj, canonical
from .log import Log, reduce
from .pieces import (
    NOW,
    READ_FILE,
    call,
    case,
    read_turn,
    reject,
    render_case,
    result,
    started,
    user,
    write_case,
)
from .tool_search import (
    COMMENT,
    CREATE,
    FAM,
    JIRA,
    SPEC_FIELDS,
    full_bytes,
    pinned,
    pinned_ref,
    search_spec,
    searched,
)
from .tool_sets import changed

if TYPE_CHECKING:
    import pathlib


def build(root: pathlib.Path) -> None:
    for name, desc, log in [*_rule46(), *_ref_forms()]:
        reject(root, (name, "log", desc), log)
    _kept(root)
    for name, desc, log, code in _imports():
        _refused(root, name, desc, log, code)


def _loaded(log: Log, call_id: str, names: list[str]) -> Obj:
    tools: list[JsonValue] = [{"name": n, "spec_ref": pinned_ref(log, n)} for n in names]
    return log.add("tools_loaded", {"call_id": call_id, "tools": tools})


def _jira() -> Log:
    log = Log()
    pinned(log, JIRA)
    user(log, "File a bug about the login page.")
    return log


def _rule46() -> list[tuple[str, str, Log]]:
    no_search = Log()
    pinned(no_search, JIRA)
    read_turn(no_search)
    _loaded(no_search, "call_1", ["mcp__jira__create_issue"])

    apart = _jira()
    searched(apart, "weather", "call_1")
    apart.model_request()
    _loaded(apart, "call_1", ["mcp__jira__create_issue"])

    failed = _jira()
    call(failed, "tool_search", {"query": "mcp__jira__create_issue"})
    result(failed, "call_1", "boom", is_error=True)
    _loaded(failed, "call_1", ["mcp__jira__create_issue"])

    plain = _jira()
    searched(plain, "weather", "call_1")
    plain.add(
        "tools_loaded",
        {
            "call_id": "call_1",
            "tools": [
                {"name": "read_file", "spec_ref": aref(canonical(READ_FILE), "application/json")}
            ],
        },
    )

    twice = _jira()
    searched(twice, "mcp__jira__create_issue", "call_1")
    searched(twice, "weather", "call_2")
    _loaded(twice, "call_2", ["mcp__jira__create_issue"])

    other = _jira()
    searched(other, "weather", "call_1")
    other.add(
        "tools_loaded",
        {
            "call_id": "call_1",
            "tools": [
                {
                    "name": "mcp__jira__create_issue",
                    "spec_ref": pinned_ref(other, "mcp__jira__add_comment"),
                }
            ],
        },
    )

    per_call = _jira()
    searched(per_call, "mcp__jira__create_issue", "call_1")
    _loaded(per_call, "call_1", ["mcp__jira__add_comment"])
    return [
        (
            "tools-loaded-without-search-rejected",
            "Rule 47: a tools_loaded after the result of a read_file call. Only a tool_search "
            "result loads deferred tools.",
            no_search,
        ),
        (
            "tools-loaded-not-adjacent-rejected",
            "Rule 47: a model_request comes between the tool_search result and the tools_loaded "
            "naming its call. A load is appended in the same batch, right after the result.",
            apart,
        ),
        (
            "tools-loaded-after-error-rejected",
            "Rule 47: the tool_search result is an error, so nothing was loaded.",
            failed,
        ),
        (
            "tools-loaded-undeferred-rejected",
            "Rule 47: a tools_loaded names read_file, which is pinned inline, not deferred in "
            "reference form.",
            plain,
        ),
        (
            "tools-loaded-twice-rejected",
            "Rule 47: a second search's tools_loaded names create_issue, already loaded on the "
            "chain.",
            twice,
        ),
        (
            "tools-loaded-ref-mismatch-rejected",
            "Rule 47: a tools_loaded names create_issue with add_comment's spec_ref, not the "
            "spec_ref create_issue was pinned with.",
            other,
        ),
        (
            "tools-loaded-per-call-twice-rejected",
            "Rule 47: a second tools_loaded for the same search call. Only one can directly "
            "follow its result.",
            per_call,
        ),
    ]


def _stubs(log: Log) -> list[JsonValue]:
    ts = obj(next(e for e in log.events if e["type"] == "thread_started")["data"])
    return arr(ts["tools"])


def _full(log: Log, name: str) -> list[JsonValue]:
    """The pinned set with `name` in its full form: the artifact's spec, as JSON."""
    full = {text(obj(t)["name"]): t for t in [CREATE, COMMENT]}
    return [full[name] if obj(t)["name"] == name else t for t in _stubs(log)]


def _ref_forms() -> list[tuple[str, str, Log]]:
    early = _jira()
    changed(early, _full(early, "mcp__jira__create_issue"))
    late = _jira()
    searched(late, "mcp__jira__create_issue", "call_1")
    changed(late, _stubs(late))
    return [
        (
            "tools-changed-full-form-before-load-rejected",
            "Rule 17 point 6: a tools_changed gives create_issue, pinned in reference form, its "
            "full form before any tools_loaded loaded it. Only tool_search un-defers a tool.",
            early,
        ),
        (
            "tools-changed-ref-form-after-load-rejected",
            "Rule 17 point 6: after create_issue is loaded, a tools_changed restates it in "
            "reference form. A loaded tool is never deferred again.",
            late,
        ),
    ]


def _kept(root: pathlib.Path) -> None:
    kept = _jira()
    changed(kept, _stubs(kept))
    write_case(
        root,
        case(
            "tools-changed-ref-form-kept",
            "log",
            "reduce",
            "Rule 17 point 6: a tools_changed restates the pinned set, each deferred tool in its "
            "reference form byte for byte, while none is loaded. It counts as equal to the pin.",
        ),
        kept,
        {"outcome": "ok", "state": reduce(kept, NOW)},
    )
    after = _jira()
    searched(after, "mcp__jira__create_issue", "call_1")
    changed(after, _full(after, "mcp__jira__create_issue"))
    render_case(
        root,
        (
            "tools-changed-full-form-after-load",
            FAM,
            "Rule 17 point 6: after create_issue is loaded, a tools_changed restates the set "
            "with create_issue in its full form, whose RFC 8785 bytes equal its spec artifact "
            "(checked at import). The next request renders the complete set from it.",
        ),
        after,
    )


def _imports() -> list[tuple[str, str, Log, str]]:
    corrupt = _bad_artifact(full_bytes(CREATE), longer=1)

    deferred = _bad_artifact(canonical({**CREATE, "defer_loading": True}))
    widens = _bad_artifact(full_bytes({**CREATE, "effect_class": "read_only"}))

    mismatch = _jira()
    searched(mismatch, "mcp__jira__create_issue", "call_1")
    other: Obj = {**CREATE, "input_schema": {"type": "object"}}
    changed(mismatch, [other if obj(t)["name"] == other["name"] else t for t in _stubs(mismatch)])
    return [
        (
            "tools-loaded-artifact-corrupt",
            "Rule 47 artifact check: create_issue's spec_ref names its artifact's sha256 with the "
            "wrong byte length. Import reads each loaded spec and verifies both, so it refuses "
            "the log with artifact_corrupt at the tools_loaded.",
            corrupt,
            "artifact_corrupt",
        ),
        (
            "tools-loaded-artifact-still-deferred-rejected",
            "Rule 47 artifact check: the spec artifact itself contains defer_loading. A loaded "
            "spec is never deferred again and never names another ref.",
            deferred,
            "invalid_transition",
        ),
        (
            "tools-loaded-artifact-widens-rejected",
            "Rule 47 artifact check: the spec artifact says read_only while the pinned stub says "
            "unguarded. An imported log can't widen a tool through its artifact.",
            widens,
            "invalid_transition",
        ),
        (
            "tools-changed-full-form-mismatch-rejected",
            "Rule 17 point 6 artifact check: after create_issue is loaded, a tools_changed gives "
            "it a full form whose input_schema differs from its spec artifact.",
            mismatch,
            "invalid_transition",
        ),
    ]


def _bad_artifact(artifact: bytes, longer: int = 0) -> Log:
    """create_issue pinned with its stub and a spec_ref naming `artifact` (its length off by
    `longer`), then loaded."""
    log = Log()
    stub: Obj = {k: CREATE[k] for k in SPEC_FIELDS if k in CREATE}
    ref = log.art(artifact, "application/json")
    ref["bytes"] = len(artifact) + longer
    started(
        log,
        [
            search_spec(["mcp__jira__create_issue"]),
            READ_FILE,
            {**stub, "defer_loading": True, "spec_ref": ref},
        ],
    )
    user(log, "File a bug.")
    searched(log, "mcp__jira__create_issue", "call_1")
    return log


def _refused(root: pathlib.Path, name: str, desc: str, log: Log, code: str) -> None:
    seq = next(
        e["seq"] for e in reversed(log.events) if e["type"] in ("tools_loaded", "tools_changed")
    )
    expected: Obj = {"outcome": "error", "error": {"code": code, "seq": seq}}
    write_case(root, case(name, "log", "render", desc), log, expected)
