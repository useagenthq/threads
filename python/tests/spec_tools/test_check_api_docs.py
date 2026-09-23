"""Every public api.json entry has an explanation, and the reference renders every one."""

import pytest
from api_docs import Entry, Json, check_docs, resolved_doc
from api_ref.members import Explain
from api_ref.pages import function_page, type_page

type Obj = dict[str, Json]

EVENTS = "urn:threads:schema:events:v1"
SCHEMAS: dict[str, Json] = {
    EVENTS: {
        "$defs": {
            "Policy": {"description": "Run policy. More."},
            "Bare": {},
            "JsonValue": {"description": "Any JSON value."},
        }
    }
}
STRING: Obj = {"prim": "string"}
THREAD: Obj = {"$ref": "#/types/Thread"}
THREAD_TYPE: Obj = {"kind": "interface", "role": "handle", "doc": "A thread. It reads."}


def member(type_: Obj, doc: str | None, *, required: bool = False, **extra: Json) -> Obj:
    """A param, field or option node; doc None leaves it undocumented."""
    return {"type": type_, "required": required, **extra, **({"doc": doc} if doc else {})}


def param(
    name: str,
    type_: Obj,
    doc: str | None,
    *,
    kind: str = "option",
    required: bool = False,
    **extra: Json,
) -> Obj:
    return {"name": name, "kind": kind, **member(type_, doc, required=required, **extra)}


def positional(name: str, type_: Obj, doc: str | None) -> Obj:
    return param(name, type_, doc, kind="positional", required=True)


def callback(*params: Obj) -> Obj:
    return {"fn": {"async": False, "params": list(params), "returns": STRING}}


def function(*params: Obj) -> Obj:
    return {
        "ts": "f",
        "py": "f",
        "package": "core",
        "async": False,
        "doc": "Does a thing.",
        "params": list(params),
        "returns": STRING,
    }


def row_type(**fields: Obj) -> Obj:
    return {"kind": "object", "casing": "api", "doc": "A row.", "fields": dict(fields)}


def contract(
    *,
    allow: str | None = "Hosts web fetch may reach.",
    fetch: str | None = "Omitted: no fetch.",
    ctx: str | None = "The run's context.",
    functions: Obj | None = None,
    types: Obj | None = None,
) -> Obj:
    """An agent with a web option two levels deep, a tool with a callback option, a Thread."""
    fetch_field = member({"object": {"allow": member({"array": STRING}, allow, default=[])}}, fetch)
    agent = function(
        param("web", {"object": {"fetch": fetch_field}}, "Web tools; omitted means none."),
        param("tags", {"array": STRING}, "Labels, any text.", default=[]),
        param("env", {"map": STRING}, "Variables, any text.", default={}),
        param("label", STRING, "A display name, any text.", default=None),
    )
    execute = callback(positional("ctx", THREAD, ctx))
    tool = function(param("execute", execute, "Runs the tool.", required=True))
    return {
        "packages": {"core": {"ts": "@threads/core", "py": "threads"}},
        "functions": {"agent": agent, "tool": tool, **(functions or {})},
        "types": {"Thread": THREAD_TYPE, **(types or {})},
    }


def problems(api: Obj) -> list[str]:
    return check_docs(api, SCHEMAS)


def with_param(p: Obj) -> Obj:
    return contract(functions={"run": function(p)})


def test_the_fixture_contract_is_fully_explained() -> None:
    assert problems(contract()) == []


def test_each_undocumented_kind_fails_with_its_name() -> None:
    close = {**function(positional("reason", STRING, None)), "ts": "close", "py": "close"}
    del close["doc"]
    handle: Obj = {
        "kind": "interface",
        "role": "handle",
        "doc": "A handle.",
        "properties": {"size": member({"prim": "integer"}, None, required=True)},
        "methods": {"close": close},
    }
    plain: Obj = {"kind": "object", "casing": "api", "fields": {}}
    row = row_type(id=member(STRING, None, required=True))
    assert problems(contract(types={"Plain": plain, "Handle": handle, "Row": row})) == [
        "api.json Plain (type): no doc",
        "api.json Handle.size (property): no doc",
        "api.json Handle.close (method): no doc",
        "api.json Handle.close.reason (param): no doc",
        "api.json Row.id (field): no doc",
    ]


def test_undocumented_nested_inputs_fail_with_their_paths() -> None:
    assert problems(contract(allow=None, fetch=None, ctx=None)) == [
        "api.json agent.web.fetch (inline_field): no doc",
        "api.json agent.web.fetch.allow (inline_field): no doc",
        "api.json tool.execute.ctx (callback_param): no doc",
    ]


def test_a_callback_inside_an_inline_object_is_walked() -> None:
    on = member(callback(positional("event", STRING, None)), "Called; omit for none.")
    api = with_param(param("hooks", {"object": {"on": on}}, "Hooks; omit for none."))
    assert problems(api) == ["api.json run.hooks.on.event (callback_param): no doc"]


def test_a_param_never_inherits_but_a_field_does() -> None:
    node = member(THREAD, None, required=True)
    types: Obj = {"Thread": THREAD_TYPE}
    assert resolved_doc(Entry(node, "param", "f.t"), types, SCHEMAS) is None
    assert resolved_doc(Entry(node, "field", "T.t"), types, SCHEMAS) == "A thread."


def test_a_gap_listed_member_still_needs_its_docs() -> None:
    # agent.browser is hidden from the reference until it is built; the gate still covers it.
    browser = param("browser", {"object": {"auth": member(STRING, None, required=True)}}, None)
    assert problems(contract(functions={"agent": function(browser)})) == [
        "api.json agent.browser (option): no doc",
        "api.json agent.browser.auth (inline_field): no doc",
    ]


def test_a_ref_typed_input_needs_its_own_doc() -> None:
    expected = ["api.json run.thread (option): no doc"]
    assert problems(with_param(param("thread", THREAD, None))) == expected


def test_an_optional_input_says_what_omitting_it_does() -> None:
    assert problems(with_param(param("thread", THREAD, "The thread to continue."))) == [
        "api.json run.thread (option): optional with no default: say what omitting it does"
    ]
    assert problems(with_param(param("thread", THREAD, "Omit it to start a new one."))) == []
    assert problems(with_param(param("name", STRING, "Any text.", default="x"))) == []


def test_a_ref_typed_field_inherits_the_first_sentence() -> None:
    assert problems(contract(types={"Row": row_type(x=member(THREAD, None))})) == []
    bare: Obj = {"kind": "interface", "role": "opaque"}
    row = row_type(x=member({"array": {"$ref": "#/types/Bare"}}, None))
    assert problems(contract(types={"Row": row, "Bare": bare})) == [
        "api.json Row.x (field): no doc",
        "api.json Bare (type): no doc",
    ]


def test_an_external_ref_field_inherits_the_schema_description() -> None:
    policy = member({"partial": {"$ref": f"{EVENTS}#/$defs/Policy"}}, None)
    bare = member({"$ref": f"{EVENTS}#/$defs/Bare"}, None)
    assert problems(contract(types={"Row": row_type(policy=policy)})) == []
    assert problems(contract(types={"Row": row_type(bare=bare)})) == [
        "api.json Row.bare (field): no doc"
    ]


def test_a_json_value_field_never_inherits() -> None:
    value = member({"$ref": f"{EVENTS}#/$defs/JsonValue"}, None)
    assert problems(contract(types={"Row": row_type(value=value)})) == [
        "api.json Row.value (field): no doc"
    ]


@pytest.mark.parametrize(
    "type_",
    [STRING, {"prim": "integer"}, {"prim": "boolean"}, {"union": [THREAD, STRING]}],
)
def test_a_primitive_or_union_needs_its_own_doc(type_: Obj) -> None:
    assert problems(with_param(positional("x", type_, None))) == ["api.json run.x (param): no doc"]
    assert problems(contract(types={"Row": row_type(x=member(type_, None))})) == [
        "api.json Row.x (field): no doc"
    ]


def test_a_whitespace_doc_is_no_doc() -> None:
    assert problems(with_param(positional("x", STRING, "  \n"))) == [
        "api.json run.x (param): no doc"
    ]


# The generator renders rows with the same rule.


def explain(api: Obj) -> Explain:
    types = api["types"]
    assert isinstance(types, dict)
    return Explain(types, SCHEMAS)


def page(api: Obj, key: str) -> str:
    functions = api["functions"]
    assert isinstance(functions, dict)
    f = functions[key]
    assert isinstance(f, dict)
    return function_page(explain(api), api, key, f)


def test_nested_inputs_and_defaults_are_rows_on_the_page() -> None:
    api = contract()
    agent = page(api, "agent")
    assert '<Field name="web.fetch.allow" type={"readonly string[]"} default={"[]"}>' in agent
    assert "  Hosts web fetch may reach.\n</Field>" in agent
    assert 'name="tags" type={"readonly string[]"} default={"[]"}>' in agent
    assert 'name="env" type={"Record<string, string>"} default={"{}"}>' in agent
    assert 'name="label" type={"string"} default={"null"}>' in agent
    tool = page(api, "tool")
    assert '<Field name="execute(ctx)" type={"Thread"} required>\n  The run\'s context.' in tool


def test_an_inherited_doc_fills_the_row() -> None:
    row = row_type(x=member(THREAD, None, required=True))
    assert '<Field name="x" type={"Thread"} required>\n  A thread.\n</Field>' in type_page(
        explain(contract()), "Row", row
    )


def test_the_generator_refuses_an_unexplained_row() -> None:
    with pytest.raises(ValueError, match=r"agent\.web\.fetch\.allow \(inline_field\) has no doc"):
        page(contract(allow=None), "agent")
