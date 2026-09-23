# pyright: strict
"""What the Python packages are missing from the contract, found by importing them.

Stdlib only; run inside the project environment (`uv run --project python`), since it imports
`threads`. Each finding is (member name, gap kind) for check_surface.py to compare with the
gaps registry.
"""

from __future__ import annotations

import dataclasses
import importlib
import inspect
import sys
import typing
from typing import TYPE_CHECKING, TypeGuard

if TYPE_CHECKING:
    from collections.abc import Callable, Collection, Mapping
    from types import ModuleType

    from surface_contract import Member

# (member name, gap kind, the other entry that exports it for a placement gap)
type Finding = tuple[str, str, str | None]
type Found = list[tuple[str, str | None]]
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

    def locate(self, package: str, name: str) -> tuple[object | None, Found]:
        """The object, and its gap: none when its declared entry exports it; placement (with
        the entry that does) when another public entry exports it; otherwise missing. A missing
        object defined in some module is still returned, so its members can be checked."""
        if (found := self.export(package, name)) is not None:
            return found, []
        for other in sorted(self.entries):
            if other != package and (found := self.export(other, name)) is not None:
                return found, [("placement", other)]
        return _defined_anywhere(name, self.root), [("missing", None)]


def _has(owner: object, name: str) -> bool:
    return hasattr(owner, name) or any(
        name in getattr(k, "__annotations__", {}) for k in inspect.getmro(_cls(owner))
    )


def _cls(owner: object) -> type:
    return owner if isinstance(owner, type) else type(owner)


def _callable_member(owner: object, name: str) -> bool:
    """A method must be callable: an attribute that merely exists is not the promised method."""
    return callable(getattr(owner, name, None))


def _required_field(owner: object, name: str) -> bool:
    """Whether a data type's field can be omitted: a dataclass or Pydantic default, or a
    TypedDict's optional key. Other classes (a Protocol, an exception) declare no default."""
    if dataclasses.is_dataclass(owner) and isinstance(owner, type):
        field = next((f for f in dataclasses.fields(owner) if f.name == name), None)
        missing = dataclasses.MISSING
        return field is None or (field.default is missing and field.default_factory is missing)
    info: object = _mapping_get(getattr(owner, "model_fields", None), name)
    if info is not None:
        is_required: object = getattr(info, "is_required", None)
        return callable(is_required) and is_required() is True
    if typing.is_typeddict(owner):
        return name in _strs(getattr(owner, "__required_keys__", None))
    return True


def _is_mapping(v: object) -> TypeGuard[Mapping[object, object]]:
    return isinstance(v, dict)


def _mapping_get(mapping: object, key: str) -> object | None:
    return mapping.get(key) if _is_mapping(mapping) else None


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

    def _capability(self, m: Member) -> tuple[object | None, Found]:
        return self.package.locate(m.package, m.capability or "")

    def _callable(self, m: Member) -> object | None:
        if m.role == "function":
            return getattr(self.package.entries[m.package], m.py, None)
        owner = self._type(m.parent)
        if m.capability is not None:
            owner = self._capability(m)[0]
        return getattr(owner, m.py, None) if owner is not None else None

    def function(self, m: Member) -> Found:
        return [] if callable(self._callable(m)) else [("missing", None)]

    def type(self, m: Member) -> Found:
        return self.package.locate(m.package, m.py)[1]

    def property(self, m: Member) -> Found:
        owner = self._type(m.parent)
        return [] if not m.required or owner is None or _has(owner, m.py) else [("missing", None)]

    def field(self, m: Member) -> Found:
        """A required field of a data type: present, and not given a default."""
        owner = self._type(m.parent)
        if not m.required or owner is None:
            return []
        if not _has(owner, m.py):
            return [("missing", None)]
        return [] if _required_field(owner, m.py) else [("required_mismatch", None)]

    def method(self, m: Member) -> Found:
        owner = self._type(m.parent)
        if owner is None:
            return []
        on_base = _callable_member(owner, m.py)
        if m.capability is None:
            return [] if on_base else [("missing", None)]
        found: Found = [("required_mismatch", None)] if on_base else []
        cap, gap = self._capability(m)
        runtime = getattr(cap, "_is_runtime_protocol", False) is True
        if not gap and not (runtime and _callable_member(cap, m.py)):
            gap = [("missing", None)]
        return found + gap

    def option(self, m: Member) -> Found:
        fn = self._callable(self.contract[m.parent])
        if not callable(fn):
            return []
        per_overload = options(fn)
        if not any(m.py in o for o in per_overload):
            return [("missing", None)]
        required = all(o.get(m.py, False) for o in per_overload)
        return [] if required == m.required else [("required_mismatch", None)]

    def findings(self) -> set[Finding]:
        checks: dict[str, Callable[[Member], Found]] = {
            "function": self.function, "type": self.type, "property": self.property,
            "field": self.field, "method": self.method, "option": self.option,
        }  # fmt: skip
        return {
            (m.name, kind, at)
            for m in self.contract.values()
            if "py" in m.langs
            for kind, at in checks[m.role](m)
        }
