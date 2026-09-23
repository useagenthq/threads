"""The built-in sandbox tools as the loop's tool runner.

Commands run through the sandbox layer's exec, so output is spilled at the source and the
model gets previews plus `full_output`. Outcomes follow a deadline or a lost
transport after dispatch is uncertain (effect_unknown, never a result), a refused fence sent
nothing. Commands run with an empty environment: no host credential enters the sandbox
(invariant 4).
"""

import posixpath
from collections.abc import Awaitable, Callable, Sequence
from typing import Final

from pydantic import BaseModel, JsonValue, ValidationError
from pydantic.experimental.missing_sentinel import MISSING
from pydantic_core import to_json

from threads._generated.tools_v1 import (
    BashInput,
    EditInput,
    GlobInput,
    GrepInput,
    LsInput,
    ReadInput,
    WriteInput,
)
from threads.log import JsonObject, ParseError, Spill, ToolSpec
from threads.log.jcs import canonicalize
from threads.loop.model import LookupResult, LookupUnknown
from threads.loop.tools import Dispatched, Invocation, NotSent, Output, Termination, Uncertain
from threads.permissions.rules import glob_matches
from threads.reduce.handlers import to_json as as_json
from threads.result import Err, Ok
from threads.sandbox.exec import Command, run_exec
from threads.sandbox.protocol import ExecResult, SandboxContext, SandboxError, SandboxSession
from threads.store import SqliteStore
from threads.tools import files
from threads.tools.specs import MODELS, PROVIDED

DEFAULT_TIMEOUT_MS: Final = 120_000
LISTING_BYTES: Final = 1 << 20
"""Listing output the host reads per stream. ponytail: a listing past 1 MiB is cut to its head
and tail; page with a narrower path."""

type Open = Callable[[], Awaitable[Ok[SandboxSession] | Err[SandboxError | ParseError]]]


class SandboxTools:
    """Runs the built-in tools in the branch's sandbox session, fenced by the run's lease. The
    session is opened on first use, so a run that never calls a built-in creates nothing."""

    def __init__(
        self, open: Open, context: SandboxContext, store: SqliteStore, limits: Callable[[], Spill]
    ) -> None:
        self._open = open
        self._session: SandboxSession | None = None
        self._context = context
        self._store = store
        self._limits = limits

    @property
    def opened(self) -> SandboxSession | None:
        """The session, once a dispatch opened it."""
        return self._session

    @property
    def _box(self) -> SandboxSession:
        if self._session is None:
            raise AssertionError("a dispatch opens the session first")
        return self._session

    async def _ensure(self) -> SandboxSession | None:
        if self._session is None:
            opened = await self._open()
            self._session = opened.value if isinstance(opened, Ok) else None
        return self._session

    async def read_file(self, path: str) -> bytes | None:
        """A framework read_only read (L3 restore): the file's bytes, or None."""
        session = await self._ensure()
        if session is None:
            return None
        got = await session.download(files.absolute(path), self._context)
        return got.value if isinstance(got, Ok) else None

    def invalid(self, spec: ToolSpec, input: JsonObject) -> str | None:
        parsed = parse(spec.name, input)
        return parsed.error if isinstance(parsed, Err) else None

    async def dispatch(self, call: Invocation) -> Dispatched:
        parsed = parse(call.spec.name, call.input)
        if isinstance(parsed, Err):
            raise AssertionError(f"a dispatched call was parsed first: {parsed.error}")
        # No session: the command was never handed to a provider.
        return NotSent() if await self._ensure() is None else await self._run(parsed.value, call)

    async def _run(self, input: BaseModel, call: Invocation) -> Dispatched:  # noqa: PLR0911 - one per tool
        match input:
            case BashInput() as args:
                return await self._bash(args, call)
            case ReadInput() as args:
                return await self._read(args)
            case WriteInput() as args:
                return await self._write(args)
            case EditInput() as args:
                return await self._edit(args)
            case LsInput() as args:
                return await self._ls(args, call)
            case GlobInput() as args:
                return await self._glob(args, call)
            case GrepInput() as args:
                return await self._grep(args, call)
            case other:
                raise AssertionError(f"no sandbox built-in takes {type(other).__name__}")

    async def lookup(self, call: Invocation) -> LookupResult[str]:
        return LookupUnknown(f"{call.spec.name} has no lookup")

    async def terminate(self, call: Invocation) -> Termination:
        session = await self._ensure()
        if session is None:
            return "unknown"
        done = await session.terminate(call.effect_key, self._context)
        return done.value if isinstance(done, Ok) else "unknown"

    def provider_now(self) -> int | None:
        return None

    async def _exec(
        self, argv: Sequence[str], key: str, timeout_ms: int = DEFAULT_TIMEOUT_MS, keep: int = 0
    ) -> Ok[ExecResult] | Err[Dispatched]:
        """`keep`: preview bytes per stream, when the host must read the output itself."""
        command = Command(argv, key, timeout_ms=timeout_ms)
        spill = await self._store.spill()
        limits = self._limits()
        if keep:
            limits = limits.model_copy(
                update={"threshold_bytes": keep, "head_bytes": keep // 2, "tail_bytes": keep // 2}
            )
        ran = await run_exec(self._box, command, self._context, spill, limits)
        return ran if isinstance(ran, Ok) else Err(outcome(ran.error))

    async def _bash(self, args: BashInput, call: Invocation) -> Dispatched:
        deadline = DEFAULT_TIMEOUT_MS if args.timeout_ms is MISSING else args.timeout_ms
        ran = await self._exec(["bash", "-c", args.command], call.effect_key, deadline)
        if isinstance(ran, Err):
            return ran.error
        result = ran.value
        body: dict[str, JsonValue] = {
            "exit_code": result.exit_code,
            "stdout": result.stdout,
            "stderr": result.stderr,
            "truncated": result.truncated,
        }
        if result.full_output is not None:
            body["full_output"] = as_json(result.full_output)
        text = to_json(body).decode("utf-8")
        return Output(text, result.exit_code != 0, result.full_output)

    async def _listing(self, argv: Sequence[str], call: Invocation) -> ExecResult | Dispatched:
        """A read-only command's result, or the outcome that stands for it."""
        ran = await self._exec(argv, call.effect_key, keep=LISTING_BYTES)
        return ran.error if isinstance(ran, Err) else ran.value

    async def _ls(self, args: LsInput, call: Invocation) -> Dispatched:
        found = await self._listing(["ls", "-1Ap", "--", files.absolute(args.path)], call)
        if not isinstance(found, ExecResult):
            return found
        if found.exit_code != 0:
            return Output(found.stderr, True)
        return Output(found.stdout or "empty directory", False, found.full_output)

    async def _matching(self, root: str, pattern: str, call: Invocation) -> list[str] | Dispatched:
        """Files under `root` whose path relative to it matches the gitignore-style `pattern`:
        `find` lists them (portable), the host matches them."""
        found = await self._listing(["find", root, "-type", "f"], call)
        if not isinstance(found, ExecResult):
            return found
        if found.exit_code != 0:
            return Output(found.stderr, True)
        names = [line for line in found.stdout.split("\n") if line]
        return [n for n in names if glob_matches(pattern, posixpath.relpath(n, root))]

    async def _glob(self, args: GlobInput, call: Invocation) -> Dispatched:
        hits = await self._matching(files.absolute(args.path), args.pattern, call)
        if not isinstance(hits, list):
            return hits
        return Output("\n".join(hits) if hits else "no files match")

    async def _grep(self, args: GrepInput, call: Invocation) -> Dispatched:
        root = files.absolute(args.path)
        only = None if args.glob is MISSING else await self._matching(root, args.glob, call)
        if only is not None and not isinstance(only, list):
            return only
        found = await self._listing(["grep", "-rnIE", "-e", args.pattern, "--", root], call)
        if not isinstance(found, ExecResult):
            return found
        # grep exits 1 for no match and 2 or more for an error.
        if found.exit_code not in (0, 1):
            return Output(found.stderr, True)
        lines = [
            line
            for line in found.stdout.split("\n")
            if line and (only is None or any(line.startswith(f"{f}:") for f in only))
        ]
        return Output("\n".join(lines) if lines else "no matches", False, found.full_output)

    async def _read(self, args: ReadInput) -> Dispatched:
        path = files.absolute(args.path)
        got = await self._box.download(path, self._context)
        if isinstance(got, Err):
            return outcome(got.error)
        body = files.text(got.value, path)
        if isinstance(body, Err):
            return Output(body.error, True)
        return Output(files.numbered(body.value, args))

    async def _write(self, args: WriteInput) -> Dispatched:
        path = files.absolute(args.path)
        if args.expected_sha256 is not MISSING:
            got = await self._box.download(path, self._context)
            if isinstance(got, Err) and got.error.code != "not_found":
                return outcome(got.error)
            now = got.value if isinstance(got, Ok) else None
            mismatch = files.stale(now, args.expected_sha256)
            if mismatch is not None:
                return Output(mismatch, True)
        return await self._upload(path, args.content)

    async def _edit(self, args: EditInput) -> Dispatched:
        path = files.absolute(args.path)
        got = await self._box.download(path, self._context)
        if isinstance(got, Err):
            return outcome(got.error)
        body = files.text(got.value, path)
        mismatch = files.stale(got.value, args.expected_sha256)
        if isinstance(body, Err) or mismatch is not None:
            return Output(body.error if isinstance(body, Err) else str(mismatch), True)
        new = files.edited(body.value, args)
        if isinstance(new, Err):
            return Output(new.error, True)
        return await self._upload(path, new.value)

    async def _upload(self, path: str, content: str) -> Dispatched:
        data = content.encode("utf-8")
        done = await self._box.upload(path, data, self._context)
        if isinstance(done, Err):
            return outcome(done.error)
        return Output(f"wrote {len(data)} bytes to {path}")


def outcome(error: SandboxError) -> Dispatched:
    """A failed provider operation as a tool outcome."""
    match error.code:
        case "timeout":
            return Uncertain("timeout")
        case "unavailable":
            return Uncertain("transport_error")
        case "stale_epoch":
            return NotSent()
        case _:
            return Output(f"{error.code}: {error.message}", True)


def parse(name: str, input: JsonObject) -> Ok[BaseModel] | Err[str]:
    """The arguments through the tool's generated input model (strict, JSON mode)."""
    text = canonicalize(dict(input))
    if not isinstance(text, Ok):
        return Err("the arguments are not canonical JSON")
    try:
        model = MODELS.get(name) or PROVIDED[name]
        return Ok(model.model_validate_json(text.value, strict=True))
    except ValidationError as error:
        return Err(f"invalid arguments for {name}: {error.error_count()} error(s): {error}")
