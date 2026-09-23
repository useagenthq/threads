"""check_surface.py over real source written with `from __future__ import annotations`: data
fields come from constructors, and a TypedDict key's top-level Required or NotRequired decides
even when the annotation is a postponed string."""

import importlib.util
import pathlib
import sys
import textwrap
from types import ModuleType

from surface_kit import core_members, problems


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


def test_a_postponed_typed_dict_may_name_types_that_exist_for_type_checking_only(
    tmp_path: pathlib.Path,
) -> None:
    # Only a key's top-level wrapper matters, so the rest of the annotation needn't resolve.
    module = source(
        tmp_path,
        """
        from typing import TYPE_CHECKING, NotRequired, TypedDict

        if TYPE_CHECKING:
            from nowhere import Checked

        class Skill(TypedDict):
            name: list[Checked]
            note: NotRequired[str]
        """,
    )
    assert problems(core=core_members() | {"Skill": module.Skill}) == []
