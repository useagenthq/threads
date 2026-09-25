"""The Python factory signature check (spec/tools/factory_surface_py.py): the real factories
match spec/api.json, and each kind of drift is red."""

import datetime
import decimal
import importlib
import json
import pathlib
from collections.abc import Callable, Sequence
from typing import NotRequired, TypedDict, Unpack

import pytest
from factory_surface_py import check_py_factory, modules_of, namespace
from gen_api_surface_factories import adapter_packages, factories
from typeexpr_render import Json, Obj, Render, obj, text

import threads
from threads import Secret

SPEC = pathlib.Path(__file__).resolve().parents[3] / "spec"
API = obj(json.loads((SPEC / "api.json").read_text()))
NS = namespace(threads)
STRING: Obj = {"prim": "string"}
SECRET: Obj = {"$ref": "#/types/Secret"}


def contract(*params: Obj) -> Obj:
    return {"ts": "f", "py": "f", "params": list(params)}


def param(
    name: str, kind: str = "option", *, required: bool = False, type_: Obj = STRING, **extra: Json
) -> Obj:
    return {"name": name, "kind": kind, "type": type_, "required": required, **extra}


def real() -> list[tuple[str, Obj, Callable[..., object]]]:
    out: list[tuple[str, Obj, Callable[..., object]]] = []
    for package in adapter_packages(API):
        module = obj(obj(API["packages"])[package]).get("py")
        for key, f in factories(API, package):
            if isinstance(module, str) and f.get("lang", "py") == "py":
                out.append((key, f, getattr(importlib.import_module(module), text(f["py"]))))
    return out


@pytest.mark.parametrize(("key", "f", "fn"), real(), ids=[k for k, _, _ in real()])
def test_each_real_factory_matches_the_contract(
    key: str, f: Obj, fn: Callable[..., object]
) -> None:
    assert check_py_factory(f, fn, NS) == [], key


def test_every_contracted_factory_is_checked() -> None:
    assert [k for k, _, _ in real()] == [
        "otel",
        "supermemory",
        "zep",
        "anthropic",
        "openai",
        "e2b",
        "daytona",
        "modal",
        "postgres",
        "mem0",
        "slack",
        "github",
        "whatsapp",
        "exa",
        "tavily",
        "brave",
    ]


def positional(api_key: Secret) -> None: ...


def two_positional(api_key: Secret, extra: str) -> None: ...


def keyword(api_key: Secret, *, region: str = "us", tries: int = 3) -> None: ...


class Opts(TypedDict):
    region: str
    tier: NotRequired[str]


def unpacked(api_key: Secret, **options: Unpack[Opts]) -> None: ...


def star(*values: str) -> None: ...


def loose(**options: str) -> None: ...


def tags(*, names: Sequence[str] = ()) -> None: ...


KEY = param("api_key", "positional", required=True, type_=SECRET)


def test_a_matching_function_passes() -> None:
    assert check_py_factory(contract(KEY), positional, NS) == []


def test_an_arity_mismatch_is_red() -> None:
    assert check_py_factory(contract(KEY), two_positional, NS) == [
        "positional params ['api_key', 'extra'] != declared ['api_key']"
    ]


def test_a_wrong_positional_type_is_red() -> None:
    wide = param("api_key", "positional", required=True)
    assert check_py_factory(contract(wide), positional, NS) == [
        "api_key: annotation <class 'threads.secrets.Secret'> != declared str"
    ]


def test_an_extra_or_missing_option_is_red() -> None:
    assert check_py_factory(contract(KEY, param("region", default="us")), keyword, NS) == [
        "options ['region', 'tries'] != declared ['region']"
    ]


def test_a_wrong_option_type_and_default_drift_are_red() -> None:
    f = contract(KEY, param("region", default="eu"), param("tries", default=3))
    assert check_py_factory(f, keyword, NS) == [
        "region: default 'us' != declared 'eu'",
        "tries: annotation <class 'int'> != declared str",
    ]


def test_typed_dict_options_are_keys_with_required_flags() -> None:
    good = contract(KEY, param("region", required=True), param("tier"))
    assert check_py_factory(good, unpacked, NS) == []
    flipped = contract(KEY, param("region"), param("tier"))
    assert check_py_factory(flipped, unpacked, NS) == ["region: required is True, declared False"]


def test_star_args_and_untyped_kwargs_are_red() -> None:
    assert check_py_factory(contract(), star, NS) == ["*values is not in the contract"]
    assert check_py_factory(contract(), loose, NS) == ["**options must be Unpack[TypedDict]"]


def priced(*, amount: decimal.Decimal) -> None: ...


def test_a_qualified_platform_type_resolves_without_the_caller_importing_it() -> None:
    f = contract(param("amount", required=True, type_={"platform": {"py": "decimal.Decimal"}}))
    assert "decimal" not in NS
    assert check_py_factory(f, priced, NS) == []
    other: Obj = {"platform": {"py": "fractions.Fraction"}}
    assert check_py_factory(contract(param("amount", required=True, type_=other)), priced, NS) == [
        "amount: annotation <class 'decimal.Decimal'> != declared fractions.Fraction"
    ]


def zoned(*, zone: datetime.tzinfo) -> None: ...


def test_a_lowercase_platform_type_imports_its_module_too() -> None:
    assert modules_of("datetime.tzinfo | collections.abc.Sequence[str]") == {
        "datetime",
        "collections.abc",
    }
    f = contract(param("zone", required=True, type_={"platform": {"py": "datetime.tzinfo"}}))
    assert check_py_factory(f, zoned, {k: v for k, v in NS.items() if k != "datetime"}) == []


def test_a_sequence_default_compares_as_a_list() -> None:
    f = contract(param("names", default=[], type_={"array": STRING}))
    assert check_py_factory(f, tags, namespace()) == []


def based(*, base_url: str | None = None) -> None: ...


def based_wrong(*, base_url: str | None = "x") -> None: ...


def test_an_optional_option_without_a_literal_default_is_t_or_none() -> None:
    f = contract(param("base_url", doc="Omitted: the SDK default."))
    assert check_py_factory(f, based, NS) == []
    assert check_py_factory(f, based_wrong, NS) == [
        "base_url: default 'x' != None (no literal default declared)"
    ]
    assert check_py_factory(f, keyword_region, NS) == [
        "base_url: annotation <class 'str'> != declared str | None",
        "base_url: default 'us' != None (no literal default declared)",
    ]


def keyword_region(*, base_url: str = "us") -> None: ...


def test_a_callback_in_a_union_is_parenthesized_in_typescript() -> None:
    """A tenant: a string, or a callback whose "no answer" is undefined in TS and None in Python."""
    param: Obj = {"name": "team_id", "kind": "positional", "type": STRING, "required": True}
    answer: Obj = {"union": [STRING, {"prim": "undefined"}]}
    fn: Obj = {"fn": {"async": False, "params": [param], "returns": answer}}
    tenant: Obj = {"union": [STRING, fn]}
    assert Render("ts").expr(tenant) == "string | ((teamId: string) => string | undefined)"
    assert Render("py").expr(tenant) == "str | Callable[[str], str | None]"
