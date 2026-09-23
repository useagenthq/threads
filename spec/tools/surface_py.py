# pyright: strict
"""What the Python packages are missing from the contract, found by importing them.

Stdlib only; run inside the project environment (`uv run --project python`), since it imports
`threads`. Each finding is (member name, gap kind, other entry) for check_surface.py to compare
with the gaps registry.

What it reads, and nothing more (spec/schema/README.md, "The API surface gate"):
- a function or method: a callable attribute;
- an option: a keyword-only parameter or an `Unpack[TypedDict]` key, and whether it has a default;
- a field of a data type: a parameter of the type's constructor (a dataclass, a Pydantic model,
  an exception) or a TypedDict key, and whether it can be omitted;
- a property of a handle or protocol: an attribute or an annotation; its required flag is not
  checked, since a Python protocol has no optional attributes.
Nothing else about a type is read: no ClassVar or annotation analysis, no signatures beyond
names and defaults. Types themselves are pyright's and the tests' job.
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

# (member name, gap kind, the other entry that exports it for a placement gap)
type Finding = tuple[str, str, str | None]
type Found = list[tuple[str, str | None]]
# A name and whether it is required (can't be omitted).
type Keys = dict[str, bool]

MISSING: Found = [("missing", None)]
MISMATCH: Found = [("required_mismatch", None)]
NAMED = (inspect.Parameter.POSITIONAL_OR_KEYWORD, inspect.Parameter.KEYWORD_ONLY)


def _is_collection(v: object) -> TypeGuard[Collection[object]]:
    return isinstance(v, list | tuple | set | frozenset)


def _strs(v: object) -> frozenset[str]:
    """The strings of a runtime collection: a module's __all__, a TypedDict's key set."""
    return frozenset(n for n in v if isinstance(n, str)) if _is_collection(v) else frozenset()


def _is_mapping(v: object) -> TypeGuard[Mapping[object, object]]:
    return isinstance(v, dict)


def _exports(module: ModuleType) -> frozenset[str]:
    return _strs(getattr(module, "__all__", ()))


class Package:
    """The imported public entries of one language's packages."""

    def __init__(self, entries: Mapping[str, str]) -> None:
        self.entries = {key: importlib.import_module(path) for key, path in entries.items()}

    def export(self, package: str, name: str) -> object | None:
        entry = self.entries[package]
        return getattr(entry, name, None) if name in _exports(entry) else None

    def locate(self, package: str, name: str) -> tuple[object | None, Found]:
        """The object, and its gap: none when its declared entry exports it; placement (with
        the entry that does) when another public entry exports it; otherwise missing, with no
        object, so its members aren't checked (the type's own gap covers them)."""
        if (found := self.export(package, name)) is not None:
            return found, []
        for other in sorted(self.entries):
            if other != package and (found := self.export(other, name)) is not None:
                return found, [("placement", other)]
        return None, MISSING


def typed_dict_keys(td: object) -> Keys:
    """A TypedDict's keys and whether each is required. Python computes the key sets from
    `total` and the Required/NotRequired it can see, which misses those written inside a
    postponed (string) annotation; so an explicit wrapper at the top of an annotation wins."""
    annotations: object = getattr(td, "__annotations__", {})
    required = _strs(getattr(td, "__required_keys__", None))
    keys: Keys = {}
    for name, annotation in annotations.items() if _is_mapping(annotations) else ():
        wrapper = _wrapper(annotation, str(getattr(td, "__module__", "")))
        if isinstance(name, str):
            keys[name] = name in required if wrapper is None else wrapper is typing.Required
    return keys


def _wrapper(annotation: object, module: str) -> object | None:
    """typing.Required or NotRequired when the annotation is wrapped in one, else None. For a
    postponed string only its head name is looked up (in the module that wrote it), so the rest
    may name types that exist for type checking only."""
    if isinstance(annotation, typing.ForwardRef):
        module = annotation.__forward_module__ or module
        annotation = annotation.__forward_arg__
    if isinstance(annotation, str):
        head = annotation.strip().strip("'\"").partition("[")[0].strip().split(".")
        found: object = vars(sys.modules[module]) if module in sys.modules else {}
        found = found.get(head[0]) if _is_mapping(found) else None
        for part in head[1:]:
            found = getattr(found, part, None)
    else:
        found = typing.get_origin(annotation)
    return found if found is typing.Required or found is typing.NotRequired else None


def data_fields(owner: object) -> Keys:
    """A data type's fields: a TypedDict's keys, else the named parameters of its constructor,
    each required when it has no default. Empty when there is no readable constructor."""
    if typing.is_typeddict(owner):
        return typed_dict_keys(owner)
    if not callable(owner):
        return {}
    try:
        signature = inspect.signature(owner)
    except (TypeError, ValueError):
        return {}
    params = signature.parameters.values()
    return {p.name: p.default is p.empty for p in params if p.kind in NAMED}


def _declares(owner: object, name: str) -> bool:
    """A handle's or protocol's property: an attribute, or an annotation on it or a base."""
    cls = owner if isinstance(owner, type) else type(owner)
    return hasattr(cls, name) or any(name in inspect.get_annotations(k) for k in cls.__mro__)


def _unpacked(fn: Callable[..., object], param: inspect.Parameter) -> Keys:
    annotation: object = param.annotation
    if isinstance(annotation, str):
        annotation = typing.get_type_hints(fn, include_extras=True).get(param.name)
    if typing.get_origin(annotation) is not typing.Unpack:
        return {}
    (td,) = typing.get_args(annotation)
    return typed_dict_keys(typing.get_origin(td) or td)


def _signature_options(fn: Callable[..., object]) -> Keys:
    """Keyword-only params and Unpack[TypedDict] keys, each with whether it is required."""
    found: Keys = {}
    for p in inspect.signature(fn).parameters.values():
        if p.kind is inspect.Parameter.KEYWORD_ONLY:
            found[p.name] = p.default is inspect.Parameter.empty
        elif p.kind is inspect.Parameter.VAR_KEYWORD:
            found |= _unpacked(fn, p)
    return found


def options(fn: Callable[..., object]) -> list[Keys]:
    """One options map per overload (the function itself when it has none)."""
    return [_signature_options(f) for f in typing.get_overloads(fn) or [fn]]


def _compare(keys: Keys | list[Keys], m: Member) -> Found:
    """A member against the names found: present somewhere, required everywhere it's required."""
    maps = keys if isinstance(keys, list) else [keys]
    if not any(m.py in k for k in maps):
        return MISSING
    return [] if all(k.get(m.py, False) for k in maps) == m.required else MISMATCH


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
        owner = self._capability(m)[0] if m.capability else self._type(m.parent)
        return getattr(owner, m.py, None)

    def function(self, m: Member) -> Found:
        return [] if callable(self._callable(m)) else MISSING

    def type(self, m: Member) -> Found:
        return self.package.locate(m.package, m.py)[1]

    def property(self, m: Member) -> Found:
        owner = self._type(m.parent)
        return [] if owner is None or _declares(owner, m.py) else MISSING

    def field(self, m: Member) -> Found:
        owner = self._type(m.parent)
        return [] if owner is None else _compare(data_fields(owner), m)

    def method(self, m: Member) -> Found:
        owner = self._type(m.parent)
        if owner is None:
            return []
        on_base = callable(getattr(owner, m.py, None))
        if m.capability is None:
            return [] if on_base else MISSING
        # The capability protocol, wherever it is exported, must be runtime-checkable and declare
        # the callable method; the base protocol must not.
        cap, gap = self._capability(m)
        runtime = getattr(cap, "_is_runtime_protocol", False) is True
        if not (runtime and callable(getattr(cap, m.py, None))) and MISSING[0] not in gap:
            gap = gap + MISSING
        return (MISMATCH if on_base else []) + gap

    def option(self, m: Member) -> Found:
        fn = self._callable(self.contract[m.parent])
        return _compare(options(fn), m) if callable(fn) else []

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
