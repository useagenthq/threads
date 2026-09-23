# pyright: strict
"""What the Python packages are missing from the contract, found by importing them.

Stdlib only; run inside the project environment (`uv run --project python`), since it imports
`threads`. Each finding is (member name, gap kind) for check_surface.py to compare with the
gaps registry.
"""

from __future__ import annotations

import importlib
import inspect
import sys
import typing
from typing import TYPE_CHECKING, TypeGuard

if TYPE_CHECKING:
    from collections.abc import Callable, Collection, Mapping
    from types import ModuleType

    from surface_contract import Member

type Finding = tuple[str, str]
type Options = dict[str, bool]


def _is_collection(v: object) -> TypeGuard[Collection[object]]:
    return isinstance(v, list | tuple | set | frozenset)


def _strs(v: object) -> frozenset[str]:
    """The strings of a runtime collection: a module's __all__, a TypedDict's key set."""
    return frozenset(n for n in v if isinstance(n, str)) if _is_collection(v) else frozenset()


def _exports(module: ModuleType) -> frozenset[str]:
    return _strs(getattr(module, "__all__", ()))


def _defined_anywhere(name: str, root: str) -> object | None:
    """The object a module of the package defines under this name, exported or not."""
    for module_name, module in sorted(sys.modules.items()):
        found: object = getattr(module, name, None)
        if module_name.partition(".")[0] == root and found is not None:
            return found
    return None


class Package:
    """The imported public entries of one language's packages."""

    def __init__(self, entries: Mapping[str, str]) -> None:
        self.entries = {key: importlib.import_module(path) for key, path in entries.items()}
        self.root = next(iter(entries.values())).partition(".")[0]

    def export(self, package: str, name: str) -> object | None:
        entry = self.entries[package]
        return getattr(entry, name, None) if name in _exports(entry) else None

    def locate(self, package: str, name: str) -> tuple[object | None, str | None]:
        """The object and its gap kind: none when exported from its declared entry."""
        if (found := self.export(package, name)) is not None:
            return found, None
        found = _defined_anywhere(name, self.root)
        return found, "missing" if found is None else "placement"


def _has(owner: object, name: str) -> bool:
    return hasattr(owner, name) or any(
        name in getattr(k, "__annotations__", {}) for k in inspect.getmro(_cls(owner))
    )


def _cls(owner: object) -> type:
    return owner if isinstance(owner, type) else type(owner)


def _unpacked(fn: Callable[..., object], param: inspect.Parameter) -> Options:
    annotation: object = param.annotation
    if isinstance(annotation, str):
        annotation = typing.get_type_hints(fn, include_extras=True).get(param.name)
    if typing.get_origin(annotation) is not typing.Unpack:
        return {}
    (td,) = typing.get_args(annotation)
    td = typing.get_origin(td) or td
    optional = dict.fromkeys(_strs(getattr(td, "__optional_keys__", None)), False)
    return optional | dict.fromkeys(_strs(getattr(td, "__required_keys__", None)), True)


def _signature_options(fn: Callable[..., object]) -> Options:
    """Keyword-only params and Unpack[TypedDict] keys, each with whether it is required."""
    found: Options = {}
    for p in inspect.signature(fn).parameters.values():
        if p.kind is inspect.Parameter.KEYWORD_ONLY:
            found[p.name] = p.default is inspect.Parameter.empty
        elif p.kind is inspect.Parameter.VAR_KEYWORD:
            found |= _unpacked(fn, p)
    return found


def options(fn: Callable[..., object]) -> list[Options]:
    """One options map per overload (the function itself when it has none)."""
    return [_signature_options(f) for f in typing.get_overloads(fn) or [fn]]


class PythonSurface:
    """Findings for one contract against the imported packages."""

    def __init__(self, contract: Mapping[str, Member], package: Package) -> None:
        self.contract = contract
        self.package = package

    def _type(self, name: str) -> object | None:
        return self.package.locate(self.contract[name].package, name)[0]

    def _callable(self, m: Member) -> object | None:
        if m.role == "function":
            return getattr(self.package.entries[m.package], m.py, None)
        owner = self._type(m.parent)
        if m.capability is not None:
            owner = self.package.locate(m.package, m.capability)[0]
        return getattr(owner, m.py, None) if owner is not None else None

    def function(self, m: Member) -> list[str]:
        return [] if self._callable(m) is not None else ["missing"]

    def type(self, m: Member) -> list[str]:
        kind = self.package.locate(m.package, m.py)[1]
        return [kind] if kind else []

    def property(self, m: Member) -> list[str]:
        owner = self._type(m.parent)
        return [] if not m.required or owner is None or _has(owner, m.py) else ["missing"]

    def method(self, m: Member) -> list[str]:
        owner = self._type(m.parent)
        if owner is None:
            return []
        on_base = _has(owner, m.py)
        if m.capability is None:
            return [] if on_base else ["missing"]
        found = ["required_mismatch"] if on_base else []
        cap, kind = self.package.locate(m.package, m.capability)
        runtime = getattr(cap, "_is_runtime_protocol", False) is True
        if kind is None and not (runtime and _has(cap, m.py)):
            kind = "missing"
        return [*found, kind] if kind else found

    def option(self, m: Member) -> list[str]:
        fn = self._callable(self.contract[m.parent])
        if not callable(fn):
            return []
        per_overload = options(fn)
        if not any(m.py in o for o in per_overload):
            return ["missing"]
        required = all(o.get(m.py, False) for o in per_overload)
        return [] if required == m.required else ["required_mismatch"]

    def findings(self) -> set[Finding]:
        checks: dict[str, Callable[[Member], list[str]]] = {
            "function": self.function, "type": self.type, "property": self.property,
            "method": self.method, "option": self.option,
        }  # fmt: skip
        return {
            (m.name, kind)
            for m in self.contract.values()
            if "py" in m.langs
            for kind in checks[m.role](m)
        }
