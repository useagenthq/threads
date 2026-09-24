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
- every optional method names its Python capability protocol (capability);
- every typed failure code is a wire ErrorCode (what the log records) or an ApiErrorCode
  from api.schema.json (what public calls return);
- every host API operation names existing api.json methods (x-api) and lists at least
  their failure codes;
- every host API route's error responses allow exactly its x-error-codes, no more, no fewer;
- api.json CaseExpectation and case.schema.json $defs/CaseExpectation stay the same shape;
- every public entry has an explanation (api_docs.py); factories follow factory_decisions.py.
"""

from __future__ import annotations

import json
import pathlib
import sys
from typing import TYPE_CHECKING

from api_docs import check_docs
from api_host import check_operations, check_route_errors
from api_schema import Json, Validator, arr, obj, pointer, strs
from factory_decisions import check_factory_contract

if TYPE_CHECKING:
    from collections.abc import Iterator

SPEC = pathlib.Path(__file__).resolve().parent.parent
API = SPEC / "api.json"
META = SPEC / "schema" / "api.schema.json"
OPENAPI = SPEC / "schema" / "host-api" / "openapi.json"
EVENTS_ID = "urn:threads:schema:events:v1"
HOST_ID = "urn:threads:schema:host-api:v1"
API_ID = "urn:threads:schema:api:v1"


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
    ids = {str(obj(d).get("$id")): p for p, d in docs.items() if "$id" in obj(d)}
    errs: list[str] = []
    for path, doc in docs.items():
        for ref, at in _refs(doc, ""):
            uri, _, frag = ref.partition("#")
            target = _target(uri, path, ids)
            if target not in docs or pointer(docs[target], frag) is None:
                errs.append(f"{path.relative_to(SPEC)}#{at}: unresolved $ref {ref}")
    return errs


def camel(py: str) -> str:
    head, *rest = py.split("_")
    return head + "".join(w[:1].upper() + w[1:] for w in rest)


def callables(api: Json) -> Iterator[tuple[str, str, dict[str, Json]]]:
    for key, f in obj(obj(api).get("functions")).items():
        yield key, key, obj(f)
    for tname, t in obj(obj(api).get("types")).items():
        for key, m in obj(obj(t).get("methods")).items():
            yield f"{tname}.{key}", key, obj(m)


def check_names(api: Json) -> list[str]:
    errs: list[str] = []
    for where, key, f in callables(api):
        ts, py = str(f.get("ts")), str(f.get("py"))
        if key != ts or ts != camel(py):
            errs.append(f"api.json {where}: key {key}, ts {ts}, py {py} (ts must be camel(py))")
        if f.get("optional") is True and not isinstance(f.get("capability"), str):
            errs.append(f"api.json {where}: an optional method needs capability (Python protocol)")
        kinds = [str(obj(p).get("kind")) for p in arr(f.get("params"))]
        if kinds != sorted(kinds, key=lambda k: k != "positional"):
            errs.append(f"api.json {where}: positional params must come before options")
    for tname, t in obj(obj(api).get("types")).items():
        members = {**obj(obj(t).get("fields")), **obj(obj(t).get("properties"))}
        errs += [
            f"api.json {tname}.{key}: capability is for optional interface properties only"
            for key, m in members.items()
            if "capability" in obj(m)
            and (key not in obj(obj(t).get("properties")) or obj(m).get("required") is not False)
        ]
    return errs


CONFORMANCE_ID = "urn:threads:schema:conformance:v1"


def check_case_expectation(api: Json, case_schema: Json) -> list[str]:
    """case.schema.json $defs/CaseExpectation is api.json CaseExpectation as JSON Schema: the
    same fields, the same required ones, arrays of the same items."""
    fields = obj(obj(obj(obj(api).get("types")).get("CaseExpectation")).get("fields"))
    schema = obj(obj(obj(case_schema).get("$defs")).get("CaseExpectation"))
    props = obj(schema.get("properties"))
    errs: list[str] = []
    if set(fields) != set(props):
        errs.append(f"CaseExpectation fields {sorted(fields)} != case.schema {sorted(props)}")
    required = {k for k, f in fields.items() if obj(f).get("required") is True}
    if required != set(strs(schema.get("required"))):
        errs.append("CaseExpectation: required fields differ between api.json and case.schema")
    for name in set(fields) & set(props):
        item = obj(obj(obj(fields[name]).get("type")).get("array")).get("$ref")
        ref = obj(obj(props[name]).get("items")).get("$ref")
        if not isinstance(ref, str) or item != CONFORMANCE_ID + ref:
            errs.append(f"CaseExpectation.{name}: items {item} != case.schema {ref}")
    return errs


def _failure_lists(node: Json, at: str) -> Iterator[tuple[list[str], str]]:
    if isinstance(node, dict):
        if ("result" in node or "stream" in node) and "errors" in node:
            yield strs(node["errors"]), at
        for k, x in node.items():
            yield from _failure_lists(x, f"{at}/{k}")
    elif isinstance(node, list):
        for i, x in enumerate(node):
            yield from _failure_lists(x, f"{at}/{i}")


def check_codes(api: Json, docs: dict[str, Json], openapi: Json) -> list[str]:
    host = docs[HOST_ID]
    known = {
        *strs(pointer(docs[EVENTS_ID], "/$defs/ErrorCode/enum")),
        *strs(pointer(docs[API_ID], "/$defs/ApiErrorCode/enum")),
        # A model send's terminal error may be a provider rejection class (the wire reasons).
        *strs(pointer(docs[EVENTS_ID], ABANDON_REASONS)),
    }
    lists = [*_failure_lists(api, "api.json")]
    lists.append(
        (strs(pointer(api, "/types/ConfigErrorCode/type/enum")), "api.json ConfigErrorCode")
    )
    lists.append((strs(pointer(host, "/$defs/RunErrorCode/enum")), "host-api RunErrorCode"))
    for path, ops in obj(obj(openapi).get("paths")).items():
        for verb, op in obj(ops).items():
            lists.append((strs(obj(op).get("x-error-codes")), f"openapi {verb} {path}"))
    return [
        f"{at}: unknown failure code {c}" for codes, at in lists for c in codes if c not in known
    ]


ABANDON_REASONS = "/$defs/ev_model_attempt_abandoned/properties/data/properties/reason/enum"
FENCED = {"SandboxContext": ("stale_epoch", "cleanup_claim_lost"), "ModelContext": ("stale_epoch",)}


def check_fence_codes(api: Json) -> list[str]:
    """An operation that takes a dispatch context declares its fence refusal among its result
    codes, or, for a stream, among its terminal error codes."""
    errs: list[str] = []
    for where, _, f in callables(api):
        for p in arr(f.get("params")):
            ref = obj(obj(p).get("type")).get("$ref")
            codes = FENCED.get(ref.removeprefix("#/types/")) if isinstance(ref, str) else None
            if codes is None:
                continue
            # A stream declares its in-band terminal error set the same way as a result does.
            listed = set(strs(obj(f.get("returns")).get("errors")))
            errs += [
                f"{where}: returns.errors lacks fence code {c}" for c in codes if c not in listed
            ]
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
    by_id = {str(obj(d).get("$id")): d for d in docs.values()}
    api, openapi = docs[API], docs[OPENAPI]
    problems = sorted(set(Validator(docs[META]).check(docs[META], api, "api.json")))
    problems += check_refs(docs)
    problems += check_names(api)
    problems += check_codes(api, by_id, openapi)
    problems += check_operations({w: f for w, _, f in callables(api)}, openapi)
    problems += check_fence_codes(api)
    problems += check_route_errors(openapi)
    problems += check_case_expectation(api, docs[SPEC / "conformance" / "case.schema.json"])
    problems += check_docs(api, by_id)
    problems += check_factory_contract(api, docs[META], SPEC)
    for p in problems:
        print(p)
    print("api contract ok" if not problems else f"{len(problems)} problem(s)")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
