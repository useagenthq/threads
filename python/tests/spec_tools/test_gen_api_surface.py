"""gen_api_surface.py: the TypeScript lines it emits for a fixture contract. Whether tsc accepts
them is proven by the compiled fixtures in typescript/packages/core/test/api-surface/."""

import pytest
from check_api import Json
from gen_api_surface import render
from surface_contract import obj
from surface_kit import api


def lines(gaps: Json = None, contract: Json = None) -> list[str]:
    return render(contract or api(), gaps or []).splitlines()


def checks(gaps: Json = None, contract: Json = None) -> dict[str, str]:
    """Each generated alias and what it asserts."""
    pairs = [line.removeprefix("export type ").partition(" = ") for line in lines(gaps, contract)]
    return {name: rhs.removesuffix(";") for name, eq, rhs in pairs if eq}


def test_every_ts_member_is_asserted() -> None:
    assert checks().keys() >= {
        "function_agent_present",
        "function_agent_overloads",
        "option_agent_model_present",
        "option_agent_model_required",
        "option_agent_name_required",
        "type_Model_exported",
        "property_Model_info_required",
        "method_Model_send_required",
        "method_Model_lookup_present",
        "method_Model_lookup_required",
        "type_Channel_exported",
    }
    assert checks()["type_Channel_exported"] == "host.Channel"


def test_lang_py_callable_not_required_in_ts() -> None:
    assert not any("runSync" in line for line in lines())


def test_an_optional_method_is_an_optional_property() -> None:
    required = checks()["method_Model_lookup_required"]
    assert required == 'Assert<Equals<Req<core.Model, "lookup">, false>>'


def test_a_missing_gap_is_asserted_missing() -> None:
    gap: Json = {"name": "Model.send", "lang": "ts", "kind": "missing", "lane": "unassigned"}
    assert checks([gap])["method_Model_send_missing"] == 'Assert<IsMissing<core.Model, "send">>'


def test_a_required_mismatch_gap_asserts_the_other_flag() -> None:
    gap: Json = {
        "name": "agent.model",
        "lang": "ts",
        "kind": "required_mismatch",
        "lane": "unassigned",
    }
    flipped = 'Assert<Equals<OptionRequired<typeof core.agent, 0, "model">, false>>'
    assert checks([gap])["option_agent_model_required"] == flipped


def test_a_type_gap_is_declared_into_its_package_and_its_members_skipped() -> None:
    gap: Json = {"name": "Model", "lang": "ts", "kind": "placement", "lane": "unassigned"}
    out = lines([gap])
    assert 'declare module "@fake/core" {' in out
    assert "  interface Model { readonly __surfaceGap: true }" in out
    assert not any(
        line.startswith(("export type method_Model", "export type property_Model")) for line in out
    )


def test_py_gaps_are_ignored() -> None:
    gap: Json = {"name": "agent", "lang": "py", "kind": "missing", "lane": "unassigned"}
    assert lines([gap]) == lines()


def test_generics_are_filled_with_never() -> None:
    contract = api()
    model = obj(obj(contract["types"])["Model"])
    model["generics"] = [{"name": "Deps"}, {"name": "Output"}]
    assert checks(contract=contract)["type_Model_exported"] == "core.Model<never, never>"


def test_an_invalid_registry_is_refused() -> None:
    with pytest.raises(SystemExit, match=r"agent\.nope is not in spec/api\.json"):
        render(
            api(), [{"name": "agent.nope", "lang": "ts", "kind": "missing", "lane": "unassigned"}]
        )
