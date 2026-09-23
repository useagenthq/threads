"""One language's spelling of the type expressions in spec/api.json."""

import json
from typing import TYPE_CHECKING

from .json_access import Json, Obj, array, obj, objs, text
from .text import camel

if TYPE_CHECKING:
    from collections.abc import Callable

# Type-expression keys, in the order they are recognized when a node has several.
KINDS = (
    "$ref",
    "prim",
    "literal",
    "enum",
    "generic",
    "array",
    "union",
    "map",
    "partial",
    "schema",
    "promise",
    "stream",
    "native",
    "object",
    "fn",
    "result",
    "type",
)

# Kinds that wrap one inner type: (TypeScript template, Python template).
WRAPPERS = {
    "map": ("Record<string, {}>", "Mapping[str, {}]"),
    "partial": ("Partial<{}>", "{}"),
    "schema": ("z.ZodType<{}>", "type[{}]"),
    "promise": ("Promise<{}>", "Awaitable[{}]"),
    "stream": ("AsyncIterable<{}>", "AsyncIterator[{}]"),
    "result": ("Result<{}>", "Ok[{}] | Err[Failure]"),
}

PRIMS = {
    "string": ("string", "str"),
    "integer": ("number", "int"),
    "number": ("number", "float"),
    "boolean": ("boolean", "bool"),
    "null": ("null", "None"),
    "bytes": ("Uint8Array", "bytes"),
    "unknown": ("unknown", "object"),
    "void": ("void", "None"),
}


def ref_name(ref: str) -> tuple[str, str | None]:
    """A readable name for a $ref, and the reference page it links to (types in this file)."""
    base, _, pointer = ref.partition("#")
    if not base:
        name = pointer.rsplit("/", 1)[-1]
        return name, f"/docs/reference/types/{name}"
    parts = [p for p in pointer.split("/") if p not in ("", "$defs", "properties")]
    if parts and parts[0].startswith("ev_"):
        parts[0] = "".join(w.capitalize() for w in parts[0][3:].split("_")) + "Event"
    return ".".join(parts), None


def type_link(t: Obj) -> str | None:
    """The reference page of the named type inside t, looking through wrappers."""
    if "$ref" in t:
        return ref_name(text(t["$ref"]))[1]
    for k in ("array", "promise", "stream", "map", "partial", "result"):
        inner = t.get(k)
        if isinstance(inner, dict):
            return type_link(inner)
    return None


def visible(member: Obj, lang: str) -> bool:
    return member.get("lang", lang) == lang


def default_text(value: Json, lang: str) -> str:
    if lang == "py":
        if value is True:
            return "True"
        if value is False:
            return "False"
        if value is None:
            return "None"
        if value == []:
            return "()"
    return json.dumps(value)


class Render:
    """One language's spelling of api.json type expressions."""

    def __init__(self, lang: str) -> None:
        self.lang = lang
        self.ts = lang == "ts"
        self._kinds: dict[str, Callable[[Obj, str], str]] = {
            "$ref": self._ref,
            "prim": self._prim,
            "literal": self._literal,
            "enum": self._enum,
            "generic": self._generic,
            "array": self._array,
            "union": self._union,
            "native": self._native,
            "object": self._object,
            "fn": self._fn,
            "type": self._type,
        }

    def key(self, name: str, casing: str) -> str:
        return camel(name) if self.ts and casing == "api" else name

    def expr(self, t: Obj, casing: str = "api") -> str:
        kind = next((k for k in KINDS if k in t), None)
        if kind is None:
            raise ValueError(f"unknown type expression {t}")
        if kind in WRAPPERS:
            template = WRAPPERS[kind][0 if self.ts else 1]
            return template.format(self.expr(obj(t[kind]), casing))
        return self._kinds[kind](t, casing)

    def returns(self, spec: Obj, is_async: bool) -> str:
        r = spec.get("returns")
        if r is None:
            ret = "void" if self.ts else "None"
        else:
            r = obj(r)
            ret = self.expr(
                {"result": {"stream": r["stream"]}} if "stream" in r and "errors" in r else r
            )
        if is_async and self.ts:
            return f"Promise<{ret}>"
        return ret

    def fn(self, f: Obj, casing: str) -> str:
        params = [p for p in objs(f.get("params", [])) if visible(p, self.lang)]
        ret = self.returns(f, bool(f.get("async")))
        if self.ts:
            args = ", ".join(
                f"{self.key(text(p['name']), casing)}: {self.expr(obj(p['type']), casing)}"
                for p in params
            )
            return f"({args}) => {ret}"
        args = ", ".join(self.expr(obj(p["type"]), casing) for p in params)
        return f"Callable[[{args}], {'Awaitable[' + ret + ']' if f.get('async') else ret}]"

    def _ref(self, t: Obj, casing: str) -> str:
        name, _ = ref_name(text(t["$ref"]))
        args = [self.expr(a, casing) for a in objs(t.get("args", []))]
        if not args:
            return name
        return f"{name}<{', '.join(args)}>" if self.ts else f"{name}[{', '.join(args)}]"

    def _prim(self, t: Obj, casing: str) -> str:
        return PRIMS[text(t["prim"])][0 if self.ts else 1]

    def _literal(self, t: Obj, casing: str) -> str:
        value = json.dumps(t["literal"])
        return value if self.ts else f"Literal[{value}]"

    def _enum(self, t: Obj, casing: str) -> str:
        values = [json.dumps(v) for v in array(t["enum"])]
        return " | ".join(values) if self.ts else f"Literal[{', '.join(values)}]"

    def _generic(self, t: Obj, casing: str) -> str:
        return text(t["generic"])

    def _array(self, t: Obj, casing: str) -> str:
        inner = self.expr(obj(t["array"]), casing)
        if self.ts:
            return f"readonly ({inner})[]" if " " in inner else f"readonly {inner}[]"
        return f"Sequence[{inner}]"

    def _union(self, t: Obj, casing: str) -> str:
        return " | ".join(self.expr(v, casing) for v in objs(t["union"]))

    def _native(self, t: Obj, casing: str) -> str:
        return text(obj(t["native"]).get(self.lang, ""))

    def _object(self, t: Obj, casing: str) -> str:
        inner = text(t.get("casing", casing))
        fields = [
            f"{self.key(k, inner)}{'' if f.get('required', True) else '?'}: "
            f"{self.expr(obj(f['type']), inner)}"
            for k, f in ((k, obj(v)) for k, v in obj(t["object"]).items())
        ]
        return "{ " + "; ".join(fields) + " }" if self.ts else "{" + ", ".join(fields) + "}"

    def _fn(self, t: Obj, casing: str) -> str:
        return self.fn(obj(t["fn"]), casing)

    def _type(self, t: Obj, casing: str) -> str:
        return self.expr(obj(t["type"]), casing)
