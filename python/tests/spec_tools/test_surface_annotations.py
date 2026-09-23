"""check_surface.py reads Python annotations the way the package declares them: postponed
strings, aliases and TypedDict Required/NotRequired, failing closed on what it can't resolve."""

import importlib.util
import pathlib
import sys
import textwrap
from types import ModuleType
from typing import ClassVar, Protocol

from surface_kit import core_members, problems

CV = ClassVar  # an alias the postponed-annotation probes resolve through
__all__ = ["CV"]


def test_a_postponed_aliased_class_variable_is_not_a_field() -> None:
    # A string annotation naming ClassVar through an alias, as `from __future__ import
    # annotations` with `from typing import ClassVar as CV` writes it.
    class Aliased:
        __annotations__ = {"name": "CV[str]", "note": "str"}
        note = ""

    Aliased.__module__ = __name__
    found = problems(core=core_members() | {"Skill": Aliased})
    assert found[0].startswith("surface gate: Skill.name (py) is missing")


def test_an_annotation_that_cannot_be_resolved_fails_closed() -> None:
    class Unresolved:
        __annotations__ = {"name": "NoSuchType", "note": "str"}
        note = ""

    found = problems(core=core_members() | {"Skill": Unresolved})
    assert found[0].startswith("surface gate: Skill.name (py) is missing")


def test_a_postponed_aliased_class_variable_protocol_property_fails() -> None:
    class InfoOnClass(Protocol):
        __annotations__ = {"info": "CV[str]"}

        def send(self) -> None: ...

    InfoOnClass.__module__ = __name__
    found = problems(core=core_members() | {"Model": InfoOnClass})
    assert found[0].startswith("surface gate: Model.info (py) is missing")


def source(tmp_path: pathlib.Path, text: str) -> ModuleType:
    """A module written with `from __future__ import annotations`, imported from its file."""
    path = tmp_path / "postponed.py"
    path.write_text("from __future__ import annotations\n" + textwrap.dedent(text))
    spec = importlib.util.spec_from_file_location(f"postponed_{tmp_path.name}", path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_a_quoted_postponed_aliased_class_variable_is_not_a_field(tmp_path: pathlib.Path) -> None:
    module = source(
        tmp_path,
        """
        from typing import ClassVar as CV

        class Skill:
            name: "CV[str]"
            note: str = ""
        """,
    )
    found = problems(core=core_members() | {"Skill": module.Skill})
    assert found[0].startswith("surface gate: Skill.name (py) is missing")


def test_a_postponed_not_required_typed_dict_option_fails(tmp_path: pathlib.Path) -> None:
    module = source(
        tmp_path,
        """
        from typing import NotRequired, TypedDict, Unpack

        class Base(TypedDict, total=False):
            name: str

        class Options(Base):
            model: NotRequired[str]

        def agent(**options: Unpack[Options]) -> None: ...
        """,
    )
    found = problems(core=core_members() | {"agent": module.agent})
    assert found[0].startswith("surface gate: agent.model (py) is required_mismatch")


def test_a_postponed_not_required_typed_dict_field_fails(tmp_path: pathlib.Path) -> None:
    module = source(
        tmp_path,
        """
        from typing import NotRequired, TypedDict

        class Skill(TypedDict):
            name: NotRequired[str]
            note: NotRequired[str]
        """,
    )
    found = problems(core=core_members() | {"Skill": module.Skill})
    assert found[0].startswith("surface gate: Skill.name (py) is required_mismatch")


def test_only_the_head_of_an_annotation_needs_to_resolve(tmp_path: pathlib.Path) -> None:
    # A type imported only for type checking is fine inside the annotation; the head decides.
    module = source(
        tmp_path,
        """
        from typing import TYPE_CHECKING

        if TYPE_CHECKING:
            from nowhere import Checked

        class Skill:
            name: list[Checked]
            note: str = ""
        """,
    )
    assert problems(core=core_members() | {"Skill": module.Skill}) == []
