#!/usr/bin/env python3
# pyright: strict
"""Check the public API contract (spec/api.json) and the host API (spec/schema/host-api/).

    python3 spec/tools/check_api.py   # exit 1 on any problem

Stdlib only, Python 3.12+. Checks:
- spec/api.json validates against spec/schema/api.schema.json (a small draft 2020-12 subset;
  any other keyword in the meta-schema is itself an error, so the subset stays honest);
- every $ref in every spec schema, api.json and openapi.json resolves (urn:threads:... $ids,
  relative file paths, JSON pointers);
- every function and method name pair is TS lowerCamel of the Python snake_case name;
- every typed failure code is a wire ErrorCode (what the log records) or an ApiErrorCode
  from api.schema.json (what public calls return);
- every host API operation names existing api.json methods (x-api) and lists at least
  their failure codes;
- every host API route's error responses allow exactly its x-error-codes, no more, no fewer;
- api.json CaseExpectation and case.schema.json $defs/CaseExpectation stay the same shape;
- every public entry has an explanation (api_docs.py).
"""

from __future__ import annotations

import json
import pathlib
import re
import sys
from typing import TYPE_CHECKING

from api_docs import check_docs

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator

type Json = dict[str, Json] | list[Json] | str | int | float | bool | None

SPEC = pathlib.Path(__file__).resolve().parent.parent
API = SPEC / "api.json"
META = SPEC / "schema" / "api.schema.json"
OPENAPI = SPEC / "schema" / "host-api" / "openapi.json"
EVENTS_ID = "urn:threads:schema:events:v1"
HOST_ID = "urn:threads:schema:host-api:v1"
API_ID = "urn:threads:schema:api:v1"
IGNORED = frozenset({"$schema", "$id", "$defs", "$comment", "title", "description", "default"})
TYPES: dict[str, Callable[[Json], bool]] = {
    "object": lambda v: isinstance(v, dict),
    "array": lambda v: isinstance(v, list),
    "string": lambda v: isinstance(v, str),
    "integer": lambda v: isinstance(v, int) and not isinstance(v, bool),
    "boolean": lambda v: isinstance(v, bool),
}


def _obj(v: Json) -> dict[str, Json]:
    return v if isinstance(v, dict) else {}


def _list(v: Json) -> list[Json]:
    return v if isinstance(v, list) else []


def _strs(v: Json) -> list[str]:
    return [x for x in _list(v) if isinstance(x, str)]


def _pointer(doc: Json, frag: str) -> Json | None:
    node: Json | None = doc
    for raw in frag.split("/")[1:] if frag else []:
        key = raw.replace("~1", "/").replace("~0", "~")
        if isinstance(node, dict):
            node = node.get(key)
        elif isinstance(node, list) and key.isdigit() and int(key) < len(node):
            node = node[int(key)]
        else:
            return None
    return node


class Validator:
    """Draft 2020-12 subset, enough for api.schema.json. $ref resolves inside the meta-schema."""

    def __init__(self, root: Json) -> None:
        self.root = root
        self.handlers: dict[str, Callable[[dict[str, Json], Json, Json, str], list[str]]] = {
            "$ref": self._ref, "type": self._type, "properties": self._properties,
            "required": self._required, "additionalProperties": self._additional,
            "propertyNames": self._names, "items": self._items, "enum": self._enum,
            "const": self._const, "oneOf": self._one_of, "anyOf": self._any_of,
            "pattern": self._pattern, "minLength": self._min_length,
            "minItems": self._min_items, "minProperties": self._min_properties,
        }  # fmt: skip

    def check(self, schema: Json, v: Json, at: str) -> list[str]:
        errs: list[str] = []
        for k, arg in _obj(schema).items():
            if k in IGNORED:
                continue
            handler = self.handlers.get(k)
            if handler is None:
                errs.append(f"api.schema.json: unsupported keyword {k}")
            else:
                errs += handler(_obj(schema), arg, v, at)
        return errs

    def _ref(self, _s: dict[str, Json], arg: Json, v: Json, at: str) -> list[str]:
        return self.check(_pointer(self.root, str(arg).partition("#")[2]), v, at)

    def _type(self, _s: dict[str, Json], arg: Json, v: Json, at: str) -> list[str]:
        return [] if TYPES[str(arg)](v) else [f"{at}: expected {arg}"]

    def _properties(self, _s: dict[str, Json], arg: Json, v: Json, at: str) -> list[str]:
        if not isinstance(v, dict):
            return []
        props = _obj(arg)
        return [e for k, x in v.items() if k in props for e in self.check(props[k], x, f"{at}/{k}")]

    def _required(self, _s: dict[str, Json], arg: Json, v: Json, at: str) -> list[str]:
        if not isinstance(v, dict):
            return []
        return [f"{at}: missing {k}" for k in _strs(arg) if k not in v]

    def _additional(self, s: dict[str, Json], arg: Json, v: Json, at: str) -> list[str]:
        if not isinstance(v, dict):
            return []
        extra = [k for k in v if k not in _obj(s.get("properties"))]
        if arg is False:
            return [f"{at}: unexpected {k}" for k in extra]
        return [e for k in extra for e in self.check(arg, v[k], f"{at}/{k}")]

    def _names(self, _s: dict[str, Json], arg: Json, v: Json, at: str) -> list[str]:
        return [e for k in _obj(v) for e in self.check(arg, k, f"{at}/{k} (name)")]

    def _items(self, _s: dict[str, Json], arg: Json, v: Json, at: str) -> list[str]:
        return [e for i, x in enumerate(_list(v)) for e in self.check(arg, x, f"{at}/{i}")]

    def _enum(self, _s: dict[str, Json], arg: Json, v: Json, at: str) -> list[str]:
        return [] if v in _list(arg) else [f"{at}: {v!r} not in {arg}"]

    def _const(self, _s: dict[str, Json], arg: Json, v: Json, at: str) -> list[str]:
        return [] if v == arg and type(v) is type(arg) else [f"{at}: expected {arg!r}"]

    def _one_of(self, _s: dict[str, Json], arg: Json, v: Json, at: str) -> list[str]:
        results = [self.check(alt, v, at) for alt in _list(arg)]
        passed = sum(1 for r in results if not r)
        if passed == 1:
            return []
        if passed > 1:
            return [f"{at}: matches {passed} oneOf alternatives"]
        return min(results, key=len)

    def _any_of(self, _s: dict[str, Json], arg: Json, v: Json, at: str) -> list[str]:
        results = [self.check(alt, v, at) for alt in _list(arg)]
        return [] if any(not r for r in results) else min(results, key=len)

    def _pattern(self, _s: dict[str, Json], arg: Json, v: Json, at: str) -> list[str]:
        ok = not isinstance(v, str) or re.search(str(arg), v)
        return [] if ok else [f"{at}: {v!r} does not match {arg}"]

    def _min_length(self, _s: dict[str, Json], arg: Json, v: Json, at: str) -> list[str]:
        ok = not isinstance(v, str) or len(v) >= int(str(arg))
        return [] if ok else [f"{at}: shorter than {arg}"]

    def _min_items(self, _s: dict[str, Json], arg: Json, v: Json, at: str) -> list[str]:
        ok = not isinstance(v, list) or len(v) >= int(str(arg))
        return [] if ok else [f"{at}: fewer than {arg} items"]

    def _min_properties(self, _s: dict[str, Json], arg: Json, v: Json, at: str) -> list[str]:
        ok = not isinstance(v, dict) or len(v) >= int(str(arg))
        return [] if ok else [f"{at}: fewer than {arg} properties"]


def _load(path: pathlib.Path) -> Json:
    doc: Json = json.loads(path.read_text())
    return doc


def _refs(node: Json, at: str) -> Iterator[tuple[str, str]]:
    if isinstance(node, dict):
        ref = node.get("$ref")
        if isinstance(ref, str):
            yield ref, at
        for k, x in node.items():
            yield from _refs(x, f"{at}/{k}")
    elif isinstance(node, list):
        for i, x in enumerate(node):
            yield from _refs(x, f"{at}/{i}")


def _target(uri: str, base: pathlib.Path, ids: dict[str, pathlib.Path]) -> pathlib.Path | None:
    if not uri:
        return base
    if uri.startswith("urn:"):
        return ids.get(uri)
    return (base.parent / uri).resolve()


def check_refs(docs: dict[pathlib.Path, Json]) -> list[str]:
    ids = {str(_obj(d).get("$id")): p for p, d in docs.items() if "$id" in _obj(d)}
    errs: list[str] = []
    for path, doc in docs.items():
        for ref, at in _refs(doc, ""):
            uri, _, frag = ref.partition("#")
            target = _target(uri, path, ids)
            if target not in docs or _pointer(docs[target], frag) is None:
                errs.append(f"{path.relative_to(SPEC)}#{at}: unresolved $ref {ref}")
    return errs


def camel(py: str) -> str:
    head, *rest = py.split("_")
    return head + "".join(w[:1].upper() + w[1:] for w in rest)


def _callables(api: Json) -> Iterator[tuple[str, str, dict[str, Json]]]:
    for key, f in _obj(_obj(api).get("functions")).items():
        yield key, key, _obj(f)
    for tname, t in _obj(_obj(api).get("types")).items():
        for key, m in _obj(_obj(t).get("methods")).items():
            yield f"{tname}.{key}", key, _obj(m)


def check_names(api: Json) -> list[str]:
    errs: list[str] = []
    for where, key, f in _callables(api):
        ts, py = str(f.get("ts")), str(f.get("py"))
        if key != ts or ts != camel(py):
            errs.append(f"api.json {where}: key {key}, ts {ts}, py {py} (ts must be camel(py))")
        kinds = [str(_obj(p).get("kind")) for p in _list(f.get("params"))]
        if kinds != sorted(kinds, key=lambda k: k != "positional"):
            errs.append(f"api.json {where}: positional params must come before options")
    return errs


CONFORMANCE_ID = "urn:threads:schema:conformance:v1"


def check_case_expectation(api: Json, case_schema: Json) -> list[str]:
    """case.schema.json $defs/CaseExpectation is api.json CaseExpectation as JSON Schema: the
    same fields, the same required ones, arrays of the same items."""
    fields = _obj(_obj(_obj(_obj(api).get("types")).get("CaseExpectation")).get("fields"))
    schema = _obj(_obj(_obj(case_schema).get("$defs")).get("CaseExpectation"))
    props = _obj(schema.get("properties"))
    errs: list[str] = []
    if set(fields) != set(props):
        errs.append(f"CaseExpectation fields {sorted(fields)} != case.schema {sorted(props)}")
    required = {k for k, f in fields.items() if _obj(f).get("required") is True}
    if required != set(_strs(schema.get("required"))):
        errs.append("CaseExpectation: required fields differ between api.json and case.schema")
    for name in set(fields) & set(props):
        item = _obj(_obj(_obj(fields[name]).get("type")).get("array")).get("$ref")
        ref = _obj(_obj(props[name]).get("items")).get("$ref")
        if not isinstance(ref, str) or item != CONFORMANCE_ID + ref:
            errs.append(f"CaseExpectation.{name}: items {item} != case.schema {ref}")
    return errs


def _failure_lists(node: Json, at: str) -> Iterator[tuple[list[str], str]]:
    if isinstance(node, dict):
        if ("result" in node or "stream" in node) and "errors" in node:
            yield _strs(node["errors"]), at
        for k, x in node.items():
            yield from _failure_lists(x, f"{at}/{k}")
    elif isinstance(node, list):
        for i, x in enumerate(node):
            yield from _failure_lists(x, f"{at}/{i}")


def check_codes(api: Json, docs: dict[str, Json], openapi: Json) -> list[str]:
    host = docs[HOST_ID]
    known = {
        *_strs(_pointer(docs[EVENTS_ID], "/$defs/ErrorCode/enum")),
        *_strs(_pointer(docs[API_ID], "/$defs/ApiErrorCode/enum")),
        # A model send's terminal error may be a provider rejection class (the wire reasons).
        *_strs(_pointer(docs[EVENTS_ID], ABANDON_REASONS)),
    }
    lists = [*_failure_lists(api, "api.json")]
    lists.append(
        (_strs(_pointer(api, "/types/ConfigErrorCode/type/enum")), "api.json ConfigErrorCode")
    )
    lists.append((_strs(_pointer(host, "/$defs/RunErrorCode/enum")), "host-api RunErrorCode"))
    for path, ops in _obj(_obj(openapi).get("paths")).items():
        for verb, op in _obj(ops).items():
            lists.append((_strs(_obj(op).get("x-error-codes")), f"openapi {verb} {path}"))
    return [
        f"{at}: unknown failure code {c}" for codes, at in lists for c in codes if c not in known
    ]


ABANDON_REASONS = "/$defs/ev_model_attempt_abandoned/properties/data/properties/reason/enum"
FENCED = {"SandboxContext": ("stale_epoch", "cleanup_claim_lost"), "ModelContext": ("stale_epoch",)}


def check_fence_codes(api: Json) -> list[str]:
    """An operation that takes a dispatch context declares its fence refusal among its result
    codes, or, for a stream, among its terminal error codes."""
    errs: list[str] = []
    for where, _, f in _callables(api):
        for p in _list(f.get("params")):
            ref = _obj(_obj(p).get("type")).get("$ref")
            codes = FENCED.get(ref.removeprefix("#/types/")) if isinstance(ref, str) else None
            if codes is None:
                continue
            # A stream declares its in-band terminal error set the same way as a result does.
            listed = set(_strs(_obj(f.get("returns")).get("errors")))
            errs += [
                f"{where}: returns.errors lacks fence code {c}" for c in codes if c not in listed
            ]
    return errs


def check_operations(api: Json, openapi: Json) -> list[str]:
    methods = {where: f for where, _, f in _callables(api)}
    errs: list[str] = []
    for path, ops in _obj(_obj(openapi).get("paths")).items():
        for verb, op in _obj(ops).items():
            codes = set(_strs(_obj(op).get("x-error-codes")))
            for name in _strs(_obj(op).get("x-api")):
                if name not in methods:
                    errs.append(f"openapi {verb} {path}: x-api {name} is not in api.json")
                    continue
                missing = set(_strs(_obj(methods[name].get("returns")).get("errors"))) - codes
                errs += [
                    f"openapi {verb} {path}: x-error-codes lacks {c} ({name})"
                    for c in sorted(missing)
                ]
    return errs


def _route_codes(response: Json, openapi: Json) -> list[str] | None:
    """The error.code enum of one error response, following a components $ref."""
    ref = _obj(response).get("$ref")
    if isinstance(ref, str):
        response = _pointer(openapi, ref.removeprefix("#"))
    schema = _pointer(response, "/content/application~1json/schema")
    for part in _list(_obj(schema).get("allOf", [])):
        codes = _pointer(part, "/properties/error/properties/code/enum")
        if codes is not None:
            return _strs(codes)
    return None


def check_route_errors(openapi: Json) -> list[str]:
    errs: list[str] = []
    for path, ops in _obj(_obj(openapi).get("paths")).items():
        for verb, op in _obj(ops).items():
            declared = set(_strs(_obj(op).get("x-error-codes")))
            allowed: set[str] = set()
            for status, response in _obj(_obj(op).get("responses")).items():
                if status.startswith("2"):
                    continue
                codes = _route_codes(response, openapi)
                if codes is None:
                    errs.append(f"openapi {verb} {path} {status}: error body has no code enum")
                else:
                    allowed.update(codes)
            if allowed != declared:
                errs.append(
                    f"openapi {verb} {path}: error codes {sorted(allowed)} "
                    f"!= x-error-codes {sorted(declared)}"
                )
    return errs


def main() -> int:
    if sys.argv[1:]:
        print(__doc__)
        return 2
    paths = [
        API,
        *sorted((SPEC / "schema").rglob("*.json")),
        SPEC / "conformance" / "case.schema.json",
    ]
    docs = {p: _load(p) for p in paths}
    by_id = {str(_obj(d).get("$id")): d for d in docs.values()}
    api, openapi = docs[API], docs[OPENAPI]
    problems = sorted(set(Validator(docs[META]).check(docs[META], api, "api.json")))
    problems += check_refs(docs)
    problems += check_names(api)
    problems += check_codes(api, by_id, openapi)
    problems += check_operations(api, openapi)
    problems += check_fence_codes(api)
    problems += check_route_errors(openapi)
    problems += check_case_expectation(api, docs[SPEC / "conformance" / "case.schema.json"])
    problems += check_docs(api, by_id)
    for p in problems:
        print(p)
    print("api contract ok" if not problems else f"{len(problems)} problem(s)")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
