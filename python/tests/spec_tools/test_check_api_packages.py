"""check_api.py's package and factory rules (spec/tools/api_factories.py): open packages,
one-language package functions, config_errors against ConfigErrorCode with the lang rule, and
"Seam:" params."""

import json
import pathlib

import pytest
from api_factories import Json, check_factories, expanded_errors
from api_schema import Validator

SPEC = pathlib.Path(__file__).resolve().parents[3] / "spec"
CODES: list[Json] = ["invalid_config", "missing_secret", "transport_fence_unsupported"]

type Obj = dict[str, Json]


def api(functions: Obj, packages: Obj | None = None) -> Obj:
    return {
        "packages": packages
        or {
            "core": {"ts": "@threads/core", "py": "threads", "kind": "core", "doc": "Core."},
            "tsonly": {"ts": "@threads/x", "kind": "adapter", "doc": "TS only."},
        },
        "functions": functions,
        "types": {"ConfigErrorCode": {"kind": "alias", "type": {"enum": CODES}}},
    }


def fn(**extra: Json) -> Obj:
    return {
        "ts": "f",
        "py": "f",
        "async": False,
        "params": [],
        "returns": {"prim": "void"},
        **extra,
    }


META: Obj = {"$defs": {"ConfigErrorCode": {"enum": CODES}}}


def problems(functions: Obj, packages: Obj | None = None, meta: Obj = META) -> list[str]:
    return check_factories(api(functions, packages), meta)


def test_a_valid_contract_has_no_problem() -> None:
    errors: Json = [{"code": "missing_secret"}, {"code": "invalid_config", "lang": "py"}]
    assert problems({"f": fn(package="core", config_errors=errors)}) == []


def test_an_unknown_package_is_refused() -> None:
    assert problems({"f": fn(package="nope")}) == ["api.json f: package nope is not in packages"]


def test_a_package_with_neither_language_is_refused() -> None:
    packages: Obj = {"core": {"kind": "core", "doc": "Nothing."}}
    assert problems({}, packages) == ["api.json packages.core: needs ts, py or both"]


def test_a_function_of_a_one_language_package_needs_lang() -> None:
    assert problems({"f": fn(package="tsonly")}) == [
        "api.json f: package tsonly exists only in ts; add lang: ts"
    ]
    assert problems({"f": fn(package="tsonly", lang="ts")}) == []
    assert problems({"f": fn(package="tsonly", lang="py")}) == [
        "api.json f: package tsonly exists only in ts; add lang: ts"
    ]


def test_an_unknown_config_error_code_is_refused() -> None:
    assert problems({"f": fn(config_errors=[{"code": "on_fire"}])}) == [
        "api.json f: config_errors code on_fire is not a ConfigErrorCode"
    ]


def test_a_lang_the_callable_does_not_exist_in_is_refused() -> None:
    errors: Json = [{"code": "missing_secret", "lang": "py"}]
    assert problems({"f": fn(package="tsonly", lang="ts", config_errors=errors)}) == [
        "api.json f: config_errors missing_secret has lang py, but f exists only in ts"
    ]


def test_lang_on_a_one_language_callable_is_redundant() -> None:
    errors: Json = [{"code": "missing_secret", "lang": "ts"}]
    assert problems({"f": fn(package="tsonly", lang="ts", config_errors=errors)}) == [
        "api.json f: config_errors missing_secret: f exists only in ts; drop lang"
    ]


def test_a_code_listed_twice_for_one_language_is_refused() -> None:
    errors: Json = [{"code": "missing_secret"}, {"code": "missing_secret", "lang": "py"}]
    assert problems({"f": fn(config_errors=errors)}) == [
        "api.json f: config_errors lists missing_secret twice for py"
    ]


def test_an_unqualified_code_on_a_ts_only_factory_expands_to_ts_only() -> None:
    f = fn(package="tsonly", lang="ts", config_errors=[{"code": "invalid_config"}])
    assert expanded_errors(f) == [("invalid_config", "ts")]


def test_an_unqualified_code_expands_to_both_languages() -> None:
    f = fn(config_errors=[{"code": "missing_secret"}, {"code": "invalid_config", "lang": "py"}])
    assert expanded_errors(f) == [
        ("invalid_config", "py"),
        ("missing_secret", "py"),
        ("missing_secret", "ts"),
    ]


def test_a_seam_param_without_lang_is_refused() -> None:
    seam: Obj = {
        "name": "transport",
        "kind": "positional",
        "type": {"platform": {"ts": "WebTransport"}},
        "required": False,
        "doc": "Seam: where requests go. Omitted: the network.",
    }
    assert problems({"f": fn(params=[seam])}) == [
        "api.json f.transport: a Seam: param exists for tests in one language; add lang"
    ]
    assert problems({"f": fn(params=[{**seam, "lang": "ts"}])}) == []


def test_the_config_error_code_mirror_must_match() -> None:
    meta: Obj = {"$defs": {"ConfigErrorCode": {"enum": ["invalid_config"]}}}
    assert problems({}, meta=meta) == [
        "api.schema.json $defs/ConfigErrorCode must equal api.json types.ConfigErrorCode"
    ]


def test_the_real_contract_passes() -> None:
    real: Json = json.loads((SPEC / "api.json").read_text())
    meta: Json = json.loads((SPEC / "schema" / "api.schema.json").read_text())
    assert check_factories(real, meta) == []


@pytest.mark.parametrize(
    ("patch", "message"),
    [
        ({"config_errors": [{"code": "on_fire"}]}, "not in"),
        ({"config_errors": [{"code": "missing_secret", "lang": "go"}]}, "not in"),
        ({"package": "Bad Name"}, "does not match"),
    ],
)
def test_the_meta_schema_rejects_malformed_factory_fields(patch: Obj, message: str) -> None:
    meta: Json = json.loads((SPEC / "schema" / "api.schema.json").read_text())
    real: Json = json.loads((SPEC / "api.json").read_text())
    assert isinstance(real, dict)
    functions = real["functions"]
    assert isinstance(functions, dict)
    exa = functions["exa"]
    assert isinstance(exa, dict)
    functions["exa"] = {**exa, **patch}
    errs = Validator(meta).check(meta, real, "api.json")
    assert any(message in e for e in errs), errs


def test_the_meta_schema_rejects_a_package_without_kind() -> None:
    meta: Json = json.loads((SPEC / "schema" / "api.schema.json").read_text())
    real: Json = json.loads((SPEC / "api.json").read_text())
    assert isinstance(real, dict)
    packages = real["packages"]
    assert isinstance(packages, dict)
    packages["search"] = {"ts": "@threads/core", "doc": "No kind."}
    errs = Validator(meta).check(meta, real, "api.json")
    assert "api.json/packages/search: missing kind" in errs
