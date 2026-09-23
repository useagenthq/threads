"""check_surface.py's Python checks, against a fixture contract and a fake package."""

from dataclasses import dataclass
from typing import Protocol, TypedDict, Unpack, runtime_checkable

from check_api import Json, check_names
from check_surface import check_python
from surface_contract import Gap, members, obj
from surface_kit import (
    ROOT,
    AgentOptions,
    LooksUp,
    Model,
    api,
    core_members,
    host_members,
    installed,
)

ENTRIES = {"core": ROOT, "host": f"{ROOT}.host"}


def problems(
    gaps: tuple[Gap, ...] = (),
    core: dict[str, object] | None = None,
    host: dict[str, object] | None = None,
    contract: Json | None = None,
) -> list[str]:
    with installed(core, host):
        return check_python(members(contract or api()), list(gaps), ENTRIES)


def without(members_: dict[str, object], name: str) -> dict[str, object]:
    return {k: v for k, v in members_.items() if k != name}


def gap(name: str, kind: str = "missing") -> Gap:
    return Gap(name, "py", kind, "unassigned")


def test_the_fixture_package_passes() -> None:
    assert problems() == []


def test_missing_function_fails() -> None:
    found = problems(core=without(core_members(), "agent"))
    assert found == [
        "surface gate: agent (py) is missing and not listed in spec/api-surface-gaps.json; "
        "fix the package, or list the gap with its owning lane"
    ]


def test_missing_required_option_fails() -> None:
    class NoModel(TypedDict, total=False):
        name: str

    def agent(**options: Unpack[NoModel]) -> None:
        del options

    found = problems(core=core_members() | {"agent": agent})
    assert [p.split(" (py)")[0] for p in found] == ["surface gate: agent.model"]
    assert "is missing" in found[0]


def test_required_flag_mismatch_fails() -> None:
    def agent(*, model: str, name: str) -> None:
        del model, name

    found = problems(core=core_members() | {"agent": agent})
    assert found[0].startswith("surface gate: agent.name (py) is required_mismatch")


def test_an_option_required_in_only_one_overload_is_not_required() -> None:
    from typing import overload  # noqa: PLC0415 - overloads register when defined

    @overload
    def agent(*, model: str) -> None: ...
    @overload
    def agent(*, model: str, name: str) -> None: ...
    def agent(*, model: str, name: str = "") -> None:
        del model, name

    assert problems(core=core_members() | {"agent": agent}) == []


def test_missing_required_method_fails() -> None:
    class Bare(Protocol):
        info: str

    found = problems(core=core_members() | {"Model": Bare})
    assert found[0].startswith("surface gate: Model.send (py) is missing")


def test_missing_required_field_fails() -> None:
    class NoInfo(Protocol):
        def send(self) -> None: ...

    found = problems(core=core_members() | {"Model": NoInfo})
    assert found[0].startswith("surface gate: Model.info (py) is missing")


def test_py_optional_method_absent_from_base_with_exported_capability_passes() -> None:
    assert problems() == []


def test_py_optional_method_absent_everywhere_fails() -> None:
    found = problems(core=without(core_members(), "LooksUp"))
    assert found[0].startswith("surface gate: Model.lookup (py) is missing")


def test_optional_method_on_base_protocol_fails() -> None:
    class WithLookup(Model, Protocol):
        def lookup(self) -> None: ...

    found = problems(core=core_members() | {"Model": WithLookup})
    assert found[0].startswith("surface gate: Model.lookup (py) is required_mismatch")


def test_capability_protocol_missing_fails() -> None:
    found = problems(core=without(core_members(), "LooksUp"))
    assert [f.split(" and ")[0] for f in found] == ["surface gate: Model.lookup (py) is missing"]


def test_capability_protocol_not_exported_fails() -> None:
    # Exported from host, not from core where the contract puts it: placement at host.
    found = problems(
        core=without(core_members(), "LooksUp"), host=host_members() | {"LooksUp": LooksUp}
    )
    assert found[0].startswith(
        "surface gate: Model.lookup (py) is placement (exported from host) and not listed"
    )
    listed = Gap("Model.lookup", "py", "placement", "unassigned", "host")
    host = host_members() | {"LooksUp": LooksUp}
    assert problems((listed,), core=without(core_members(), "LooksUp"), host=host) == []


def test_capability_protocol_without_method_fails() -> None:
    @runtime_checkable
    class LooksUpNothing(Protocol):
        pass

    found = problems(core=core_members() | {"LooksUp": LooksUpNothing})
    assert found[0].startswith("surface gate: Model.lookup (py) is missing")


def test_capability_protocol_must_be_runtime_checkable() -> None:
    class Static(Protocol):
        def lookup(self) -> None: ...

    found = problems(core=core_members() | {"LooksUp": Static})
    assert found[0].startswith("surface gate: Model.lookup (py) is missing")


def test_optional_method_without_capability_in_contract_fails() -> None:
    contract = api()
    lookup = obj(obj(obj(obj(contract["types"])["Model"])["methods"])["lookup"])
    del lookup["capability"]
    assert check_names(contract) == [
        "api.json Model.lookup: an optional method needs capability (Python protocol)"
    ]


def test_lang_py_callable_missing_in_py_fails() -> None:
    found = problems(core=without(core_members(), "run_sync"))
    assert found[0].startswith("surface gate: runSync (py) is missing")


def test_lang_ts_callable_is_not_checked_in_py() -> None:
    contract = api()
    obj(obj(contract["functions"])["runSync"])["lang"] = "ts"
    assert problems(core=without(core_members(), "run_sync"), contract=contract) == []


def test_type_missing_from_declared_package_fails() -> None:
    assert problems(host={})[0].startswith("surface gate: Channel (py) is missing")
    elsewhere = problems(host={}, core=core_members() | host_members())
    assert elsewhere[0].startswith("surface gate: Channel (py) is placement (exported from core)")


def test_type_reexported_elsewhere_passes() -> None:
    assert problems(core=core_members() | host_members()) == []


def test_unlisted_gap_fails_and_a_listed_one_passes() -> None:
    core = without(core_members(), "agent")
    assert problems(core=core) != []
    assert problems((gap("agent"),), core=core) == []


def test_stale_gap_entry_fails() -> None:
    assert problems((gap("agent"),)) == [
        "surface gate: agent (py, missing, lane unassigned) is listed but fixed; delete its entry"
    ]


def test_options_are_read_from_unpacked_typed_dicts() -> None:
    from surface_py import options  # noqa: PLC0415 - imports the checker's helpers

    def fn(*, flag: bool = False, **rest: Unpack[AgentOptions]) -> None:
        del flag, rest

    assert options(fn) == [{"flag": False, "model": True, "name": False}]


def test_a_required_field_of_a_data_type_deleted_fails() -> None:
    @dataclass(frozen=True, slots=True)
    class NoName:
        note: str = ""

    found = problems(core=core_members() | {"Skill": NoName})
    assert found[0].startswith("surface gate: Skill.name (py) is missing")


def test_a_required_field_made_optional_fails() -> None:
    @dataclass(frozen=True, slots=True)
    class OptionalName:
        name: str = ""
        note: str = ""

    found = problems(core=core_members() | {"Skill": OptionalName})
    assert found[0].startswith("surface gate: Skill.name (py) is required_mismatch")


def test_a_function_that_is_not_callable_fails() -> None:
    found = problems(core=core_members() | {"agent": 0})
    assert found[0].startswith("surface gate: agent (py) is missing")


def test_a_method_that_is_not_callable_fails() -> None:
    class ValueSend(Protocol):
        info: str
        send = 0

    found = problems(core=core_members() | {"Model": ValueSend})
    assert found[0].startswith("surface gate: Model.send (py) is missing")
