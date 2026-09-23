# pyright: strict
"""What the Python packages are missing from the contract, found by importing them.

Stdlib only; run inside the project environment (`uv run --project python`), since it imports
`threads`. Each finding is (member name, gap kind) for check_surface.py to compare with the
gaps registry.
"""

from __future__ import annotations

import ast
import builtins
import dataclasses
import importlib
import inspect
import sys
import typing
from typing import TYPE_CHECKING, Literal, TypeGuard

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


def _cls(owner: object) -> type:
    return owner if isinstance(owner, type) else type(owner)


def _callable_member(owner: object, name: str) -> bool:
    """A method must be callable: an attribute that merely exists is not the promised method."""
    return callable(getattr(owner, name, None))


type State = Literal["absent", "required", "optional"]


def _state(required: bool) -> State:
    return "required" if required else "optional"


def field_state(owner: object, name: str) -> State:
    """Whether a field or property is declared, and whether it can be omitted. A dataclass or
    Pydantic model counts only its real fields (a ClassVar or a plain attribute is not one), a
    TypedDict its keys; any other class (a Protocol, an exception) its instance attributes."""
    if dataclasses.is_dataclass(owner) and isinstance(owner, type):
        field = next((f for f in dataclasses.fields(owner) if f.name == name), None)
        if field is None:
            return "absent"
        missing = dataclasses.MISSING
        return _state(field.default is missing and field.default_factory is missing)
    model_fields: object = getattr(owner, "model_fields", None)
    if _is_mapping(model_fields):
        info = model_fields.get(name)
        is_required: object = getattr(info, "is_required", None)
        return "absent" if info is None else _state(callable(is_required) and is_required() is True)
    if typing.is_typeddict(owner):
        keys = typed_dict_keys(owner)
        return "absent" if name not in keys else _state(keys[name])
    return _attribute_state(_cls(owner), name)


def typed_dict_keys(td: object) -> dict[str, bool]:
    """A TypedDict's keys and whether each is required. An explicit Required or NotRequired
    decides, else the key set Python computed from total (with postponed annotations those sets
    miss Required and NotRequired written as strings). A key whose annotation can't be read is
    left out, which fails closed."""
    required = _strs(getattr(td, "__required_keys__", None))
    keys: dict[str, bool] = {}
    for base in reversed(_typed_dict_bases(td)):
        for name, annotation in _own_heads(base).items():
            if annotation is typing.Required or annotation is typing.NotRequired:
                keys[name] = annotation is typing.Required
            elif annotation is not UNRESOLVED:
                keys[name] = name in required
    return keys


def _objects(v: object) -> list[object]:
    return list(v) if _is_collection(v) else []


def _typed_dict_bases(td: object) -> list[type]:
    """The TypedDict and the TypedDicts it extends, nearest first (a TypedDict's MRO is only
    itself and dict, so its bases are read from __orig_bases__)."""
    found: list[type] = []
    pending: list[object] = [td]
    while pending:
        current = pending.pop(0)
        if isinstance(current, type) and typing.is_typeddict(current) and current not in found:
            found.append(current)
            bases: object = getattr(current, "__orig_bases__", ())
            pending += _objects(bases)
    return found


def _attribute_state(cls: type, name: str) -> State:
    """An instance annotation (not a ClassVar) is required unless the class gives it a default;
    a property is required; a plain class attribute is only a default, so optional. An
    annotation that can't be resolved fails closed: the member counts as undeclared."""
    annotated = [a[name] for a in map(_own_heads, inspect.getmro(cls)) if name in a]
    if annotated and annotated[0] in (UNRESOLVED, typing.ClassVar):
        return "absent"
    if isinstance(inspect.getattr_static(cls, name, None), property):
        return "required"
    if annotated:
        return _state(not hasattr(cls, name))
    return "optional" if hasattr(cls, name) else "absent"


UNRESOLVED = object()


def _own_heads(cls: type) -> dict[str, object]:
    """One class's own annotated names, each mapped to what its annotation is at the top level:
    ClassVar, Required or NotRequired for those wrappers, else the annotation's origin or the
    annotation itself. Only the head is resolved, so a type imported for checking only doesn't
    matter. A postponed string, a string quoted inside one, and an alias such as
    `ClassVar as CV` resolve in the class's module; a head that can't be resolved is UNRESOLVED,
    which fails closed."""
    scope = vars(sys.modules[cls.__module__]) if cls.__module__ in sys.modules else {}
    return {name: _head(a, scope) for name, a in inspect.get_annotations(cls).items()}


def _head(annotation: object, scope: Mapping[str, object]) -> object:
    if isinstance(annotation, typing.ForwardRef):  # how a TypedDict keeps a postponed string
        annotation = annotation.__forward_arg__
    if not isinstance(annotation, str):
        return typing.get_origin(annotation) or annotation
    try:
        node = ast.parse(annotation, mode="eval").body
    except SyntaxError:
        return UNRESOLVED
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return _head(node.value, scope)
    return _resolve(node.value if isinstance(node, ast.Subscript) else node, scope)


def _resolve(node: ast.expr, scope: Mapping[str, object]) -> object:
    """A dotted name looked up in a module's namespace, never evaluated as code."""
    if isinstance(node, ast.Name):
        return scope.get(node.id, getattr(builtins, node.id, UNRESOLVED))
    if isinstance(node, ast.Attribute):
        base = _resolve(node.value, scope)
        return UNRESOLVED if base is UNRESOLVED else getattr(base, node.attr, UNRESOLVED)
    return UNRESOLVED


def _is_mapping(v: object) -> TypeGuard[Mapping[object, object]]:
    return isinstance(v, dict)


def _unpacked(fn: Callable[..., object], param: inspect.Parameter) -> Options:
    annotation: object = param.annotation
    if isinstance(annotation, str):
        annotation = typing.get_type_hints(fn, include_extras=True).get(param.name)
    if typing.get_origin(annotation) is not typing.Unpack:
        return {}
    (td,) = typing.get_args(annotation)
    return typed_dict_keys(typing.get_origin(td) or td)


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

    def field(self, m: Member) -> Found:
        """A field of a data type or a property of a handle or protocol: declared, and
        omittable exactly when the contract says it is optional."""
        owner = self._type(m.parent)
        if owner is None:
            return []
        state = field_state(owner, m.py)
        if state == "absent":
            return [("missing", None)]
        return [] if (state == "required") == m.required else [("required_mismatch", None)]

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
        # Wherever the protocol is exported (even from another entry), it must declare the method.
        if not (runtime and _callable_member(cap, m.py)) and ("missing", None) not in gap:
            gap = [*gap, ("missing", None)]
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
            "function": self.function, "type": self.type, "property": self.field,
            "field": self.field, "method": self.method, "option": self.option,
        }  # fmt: skip
        return {
            (m.name, kind, at)
            for m in self.contract.values()
            if "py" in m.langs
            for kind, at in checks[m.role](m)
        }
