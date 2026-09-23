"""A saved case's log, once per implementation (spec/conformance/README.md): the same export
with the last segment's header naming that writer, and that segment's chain and head
recomputed. Every other byte is unchanged."""

from typing import Final, Literal

from pydantic import JsonValue, TypeAdapter

from threads.log import BranchId, ParseError
from threads.log.digest import sha256_hex
from threads.log.jcs import canonicalize
from threads.result import Err, Ok
from threads.store import verify_export
from threads.store.lines import head_line

type Impl = Literal["threads-py", "threads-ts"]
IMPLS: Final[tuple[Impl, ...]] = ("threads-py", "threads-ts")
_REF_KEYS: Final = frozenset({"sha256", "bytes", "media_type"})
_JSON: TypeAdapter[JsonValue] = TypeAdapter(JsonValue)


def per_impl(export: bytes, impl: Impl, now: int) -> Ok[tuple[bytes, JsonValue]] | Err[ParseError]:
    """The export rewritten for `impl`'s writer, and the state a reader reduces it to. The
    result is read back through the storage boundary, so it is a valid export or an error."""
    lines = [_object(line) for line in export.splitlines()[:-1]]
    last = max(i for i, line in enumerate(lines) if line.get("format") == "threads.log")
    writer = _object_of(lines[last].get("writer"))
    lines[last] = {**lines[last], "writer": {**writer, "impl": impl}}
    out = [_canonical(line) for line in lines[: last + 1]]
    for line in lines[last + 1 :]:
        out.append(_canonical({**line, "prev_hash": sha256_hex(out[-1])}))
    tail = lines[-1]
    branch, seq = BranchId(str(tail["branch_id"])), tail.get("seq", 0)
    head = head_line(branch, seq if isinstance(seq, int) else 0, sha256_hex(out[-1]))
    data = b"".join(line + b"\n" for line in (*out, head))
    read = verify_export(data, now)
    return read if isinstance(read, Err) else Ok((data, read.value.state.to_json()))


def artifact_refs(export: bytes) -> list[str]:
    """The sha256 of every ArtifactRef the export's lines carry, sorted."""
    refs: set[str] = set()
    for line in export.splitlines():
        _refs(_JSON.validate_json(line), refs)
    return sorted(refs)


def _refs(value: JsonValue, into: set[str]) -> None:
    if isinstance(value, list):
        for item in value:
            _refs(item, into)
    elif isinstance(value, dict):
        sha = value.get("sha256")
        if frozenset(value) == _REF_KEYS and isinstance(sha, str):
            into.add(sha)
        for item in value.values():
            _refs(item, into)


def _object(line: bytes) -> dict[str, JsonValue]:
    return _object_of(_JSON.validate_json(line))


def _object_of(value: JsonValue) -> dict[str, JsonValue]:
    if not isinstance(value, dict):
        raise TypeError("an exported line is a JSON object")
    return value


def _canonical(value: JsonValue) -> bytes:
    match canonicalize(value):
        case Ok(value=text):
            return text.encode("utf-8")
        case Err(error=reason):
            raise ValueError(reason)
