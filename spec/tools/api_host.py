# pyright: strict
"""The host API checks of check_api.py: every operation in spec/schema/host-api/openapi.json names
existing api.json methods (x-api) and lists at least their failure codes, and every route's
error responses allow exactly its x-error-codes. Stdlib only."""

from __future__ import annotations

from api_schema import Json, arr, obj, pointer, strs


def check_operations(methods: dict[str, dict[str, Json]], openapi: Json) -> list[str]:
    """`methods`: every api.json function and method by its name (check_api.callables)."""
    errs: list[str] = []
    for path, ops in obj(obj(openapi).get("paths")).items():
        for verb, op in obj(ops).items():
            codes = set(strs(obj(op).get("x-error-codes")))
            for name in strs(obj(op).get("x-api")):
                if name not in methods:
                    errs.append(f"openapi {verb} {path}: x-api {name} is not in api.json")
                    continue
                missing = set(strs(obj(methods[name].get("returns")).get("errors"))) - codes
                errs += [
                    f"openapi {verb} {path}: x-error-codes lacks {c} ({name})"
                    for c in sorted(missing)
                ]
    return errs


def _route_codes(response: Json, openapi: Json) -> list[str] | None:
    """The error.code enum of one error response, following a components $ref."""
    ref = obj(response).get("$ref")
    if isinstance(ref, str):
        response = pointer(openapi, ref.removeprefix("#"))
    schema = pointer(response, "/content/application~1json/schema")
    for part in arr(obj(schema).get("allOf", [])):
        codes = pointer(part, "/properties/error/properties/code/enum")
        if codes is not None:
            return strs(codes)
    return None


def check_route_errors(openapi: Json) -> list[str]:
    errs: list[str] = []
    for path, ops in obj(obj(openapi).get("paths")).items():
        for verb, op in obj(ops).items():
            declared = set(strs(obj(op).get("x-error-codes")))
            allowed: set[str] = set()
            for status, response in obj(obj(op).get("responses")).items():
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
