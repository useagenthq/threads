# pyright: strict
"""The Python half of the factory signature checks (lane 15), run at test time.

For a factory in spec/api.json and the Python function that implements it:
- positional params: exactly the declared ones, in order, with their required flags;
- options: keyword-only params plus the keys of an `**options: Unpack[TypedDict]`, exactly the
  declared set, with their required flags; no *args, and **kwargs only as Unpack[TypedDict];
- every annotation equals the declared type, spelled by typeexpr_render and evaluated in the
  given namespace (so `str | Secret` must be exactly that, not wider);
- an explicit keyword-only default equals the declared literal; an optional keyword-only
  param without one is `T | None = None`. Defaults inside a TypedDict can't
  be introspected: they are proven by behavior tests.

pyright proves the return type over the file gen_api_surface_factories.py writes. Stdlib only.
"""

from __future__ import annotations

import collections.abc
import importlib
import inspect
import re
import typing
from typing import TYPE_CHECKING

from typeexpr_render import Obj, Render, obj, objs, text

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping

    from typeexpr_render import Json

EMPTY = inspect.Parameter.empty
# A dotted name in a platform spelling: its module is everything before the last dot.
DOTTED = re.compile(r"\b[A-Za-z_]\w*(?:\.[A-Za-z_]\w*)+")
POSITIONAL = (inspect.Parameter.POSITIONAL_ONLY, inspect.Parameter.POSITIONAL_OR_KEYWORD)


def namespace(*modules: object) -> dict[str, object]:
    """Names a rendered Python type can use: typing, collections.abc (which wins, as the code
    base spells Sequence and friends from there), then the given modules' attributes."""
    out: dict[str, object] = {**vars(typing), **vars(collections.abc)}
    for m in modules:
        out.update(vars(m))
    return out


def _declared(f: Obj, kind: str) -> list[Obj]:
    return [
        p for p in objs(f.get("params", [])) if p["kind"] == kind and p.get("lang", "py") == "py"
    ]


def _options(
    fn: Callable[..., object], sig: inspect.Signature
) -> tuple[dict[str, object], dict[str, bool], dict[str, object], list[str]]:
    """Each option's annotation, required flag and explicit default, and shape problems."""
    hints = typing.get_type_hints(fn)
    types: dict[str, object] = {}
    required: dict[str, bool] = {}
    defaults: dict[str, object] = {}
    errs: list[str] = []
    for p in sig.parameters.values():
        if p.kind is inspect.Parameter.KEYWORD_ONLY:
            types[p.name], required[p.name] = hints.get(p.name), p.default is EMPTY
            if p.default is not EMPTY:
                defaults[p.name] = p.default
        elif p.kind is inspect.Parameter.VAR_KEYWORD:
            unpacked = typing.get_args(typing.get_type_hints(fn, include_extras=True).get(p.name))
            if len(unpacked) != 1 or not typing.is_typeddict(unpacked[0]):
                errs.append(f"**{p.name} must be Unpack[TypedDict]")
                continue
            td: type = unpacked[0]
            keys = typing.get_type_hints(td)
            must: frozenset[str] = getattr(td, "__required_keys__", frozenset())
            types.update(keys)
            required.update({k: k in must for k in keys})
        elif p.kind is inspect.Parameter.VAR_POSITIONAL:
            errs.append(f"*{p.name} is not in the contract")
    return types, required, defaults, errs


def _positional_problems(
    f: Obj, sig: inspect.Signature, hints: Mapping[str, object], ns: dict[str, object]
) -> list[str]:
    declared = _declared(f, "positional")
    actual = [p for p in sig.parameters.values() if p.kind in POSITIONAL]
    names = [text(p["name"]) for p in declared]
    if [p.name for p in actual] != names:
        return [f"positional params {[p.name for p in actual]} != declared {names}"]
    errs: list[str] = []
    for want, got in zip(declared, actual, strict=True):
        if (got.default is EMPTY) != bool(want["required"]):
            errs.append(
                f"{got.name}: required is {got.default is EMPTY}, declared {want['required']}"
            )
        # As for options: an optional positional with no literal default is `T | None = None`.
        or_none = not want["required"] and "default" not in want
        errs += _type_problem(got.name, hints.get(got.name), want, ns, or_none=or_none)
        if or_none and got.default is not EMPTY and got.default is not None:
            errs.append(
                f"{got.name}: default {got.default!r} != None (no literal default declared)"
            )
    return errs


def modules_of(platform: str) -> set[str]:
    """The modules a Python platform spelling names: `httpx` in `httpx.AsyncBaseTransport`,
    `datetime` in `datetime.tzinfo`."""
    return {m.group(0).rpartition(".")[0] for m in DOTTED.finditer(platform)}


def _platforms(t: Json) -> list[str]:
    """The Python platform spellings inside a type expression."""
    if isinstance(t, list):
        return [s for v in t for s in _platforms(v)]
    if not isinstance(t, dict):
        return []
    own = t.get("platform")
    here = [py] if isinstance(own, dict) and isinstance(py := own.get("py"), str) else []
    return here + [s for v in t.values() for s in _platforms(v)]


def _type_problem(
    name: str, got: object, want: Obj, ns: dict[str, object], *, or_none: bool = False
) -> list[str]:
    spelled = Render("py").expr(obj(want["type"])) + (" | None" if or_none else "")
    scope = dict(ns)
    for module in {m for p in _platforms(want["type"]) for m in modules_of(p)}:
        top = module.partition(".")[0]
        importlib.import_module(module)
        scope.setdefault(top, importlib.import_module(top))
    expected: object = eval(spelled, scope)  # noqa: S307 - our own contract's rendered type
    return [] if got == expected else [f"{name}: annotation {got!r} != declared {spelled}"]


def _py(default: Json) -> object:
    """A JSON array default is a tuple in Python (immutable by default)."""
    return tuple(default) if isinstance(default, list) else default


def check_py_factory(f: Obj, fn: Callable[..., object], ns: dict[str, object]) -> list[str]:
    """Problems with fn as the Python implementation of api.json function f; [] when it matches."""
    sig = inspect.signature(fn)
    errs = _positional_problems(f, sig, typing.get_type_hints(fn), ns)
    types, required, defaults, more = _options(fn, sig)
    errs += more
    declared = {text(p["name"]): p for p in _declared(f, "option")}
    if set(types) != set(declared):
        errs.append(f"options {sorted(types)} != declared {sorted(declared)}")
    for name in sorted(set(types) & set(declared)):
        want = declared[name]
        if required[name] != bool(want["required"]):
            errs.append(f"{name}: required is {required[name]}, declared {want['required']}")
        # An optional keyword-only param with no literal default is `T | None = None`: omitting
        # it means what its doc says (the docs signature spells it the same way).
        or_none = name in defaults and not want["required"] and "default" not in want
        errs += _type_problem(name, types[name], want, ns, or_none=or_none)
        if or_none and defaults[name] is not None:
            errs.append(f"{name}: default {defaults[name]!r} != None (no literal default declared)")
        if name in defaults and "default" in want and defaults[name] != _py(want["default"]):
            errs.append(f"{name}: default {defaults[name]!r} != declared {want['default']!r}")
    return errs
