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
from pydantic_core import to_json

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
from threads.tools.specs import (
    BashInput,
    EditInput,
    GlobInput,
    GrepInput,
    LsInput,
    ReadInput,
    WriteInput,
    input_model,
)

DEFAULT_TIMEOUT_MS: Final = 120_000


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
    def _box(self) -> SandboxSession:
        if self._session is None:
            raise AssertionError("a dispatch opens the session first")
        return self._session

    async def _ensure(self) -> SandboxSession | None:
        if self._session is None:
            opened = await self._open()
            self._session = opened.value if isinstance(opened, Ok) else None
        return self._session

    def invalid(self, spec: ToolSpec, input: JsonObject) -> str | None:
        parsed = _parse(spec.name, input)
        return parsed.error if isinstance(parsed, Err) else None

    async def dispatch(self, call: Invocation) -> Dispatched:
        parsed = _parse(call.spec.name, call.input)
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
                raise AssertionError(f"no built-in takes {type(other).__name__}")

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
        self, argv: Sequence[str], key: str, timeout_ms: int | None = None
    ) -> Ok[ExecResult] | Err[Dispatched]:
        command = Command(argv, key, timeout_ms=timeout_ms or DEFAULT_TIMEOUT_MS)
        spill = await self._store.spill()
        ran = await run_exec(self._box, command, self._context, spill, self._limits())
        return ran if isinstance(ran, Ok) else Err(outcome(ran.error))

    async def _bash(self, args: BashInput, call: Invocation) -> Dispatched:
        ran = await self._exec(["bash", "-c", args.command], call.effect_key, args.timeout_ms)
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

    async def _listing(self, argv: Sequence[str], call: Invocation, empty: str) -> Dispatched:
        """A read-only command: its stdout is the result; exit 1 with no output is `empty`."""
        ran = await self._exec(argv, call.effect_key)
        if isinstance(ran, Err):
            return ran.error
        result = ran.value
        if result.exit_code not in (0, 1) or (result.exit_code == 1 and result.stderr):
            return Output(result.stderr or f"exit {result.exit_code}", True)
        return Output(result.stdout or empty, False, result.full_output)

    async def _ls(self, args: LsInput, call: Invocation) -> Dispatched:
        return await self._listing(["ls", "-1Ap", "--", files.relative(args.path)], call, "")

    async def _glob(self, args: GlobInput, call: Invocation) -> Dispatched:
        """`find` lists the files (portable, unlike bash globstar); the pattern is matched on
        the host with the permission rules' gitignore globs, relative to `path`."""
        root = files.relative(args.path)
        found = await self._listing(["find", root, "-type", "f"], call, "")
        if not isinstance(found, Output) or found.is_error:
            return found
        names = [posixpath.normpath(line) for line in found.text.splitlines()]
        hits = [n for n in names if glob_matches(args.pattern, posixpath.relpath(n, root))]
        more = "\n[listing truncated; narrow path]" if found.full_output is not None else ""
        return Output("\n".join(sorted(hits)) + more if hits else "no files" + more)

    async def _grep(self, args: GrepInput, call: Invocation) -> Dispatched:
        argv = ["grep", "-rnIE"]
        if args.glob is not None:
            argv.append(f"--include={args.glob}")
        argv += ["--", args.pattern, files.relative(args.path)]
        found = await self._listing(argv, call, "no matches")
        if not isinstance(found, Output) or found.is_error:
            return found
        lines = found.text.splitlines(keepends=True)
        text = "".join(line.removeprefix("./") for line in lines)
        return Output(text, False, found.full_output)

    async def _read(self, args: ReadInput) -> Dispatched:
        got = await self._box.download(files.absolute(args.path), self._context)
        if isinstance(got, Err):
            return outcome(got.error)
        shown = files.numbered(got.value, args)
        return Output(shown.value) if isinstance(shown, Ok) else Output(shown.error, True)

    async def _write(self, args: WriteInput) -> Dispatched:
        path = files.absolute(args.path)
        current = None
        if args.expected_sha256 is not None:
            got = await self._box.download(path, self._context)
            if isinstance(got, Err) and got.error.code != "not_found":
                return outcome(got.error)
            current = got.value if isinstance(got, Ok) else None
        data = files.written(current, args)
        if isinstance(data, Err):
            return Output(data.error, True)
        return await self._upload(path, data.value)

    async def _edit(self, args: EditInput) -> Dispatched:
        path = files.absolute(args.path)
        got = await self._box.download(path, self._context)
        if isinstance(got, Err):
            return outcome(got.error)
        data = files.edited(got.value, args)
        if isinstance(data, Err):
            return Output(data.error, True)
        return await self._upload(path, data.value)

    async def _upload(self, path: str, data: bytes) -> Dispatched:
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
            return Output(files.failure(error), True)


def _parse(name: str, input: JsonObject) -> Ok[BaseModel] | Err[str]:
    text = canonicalize(dict(input))
    if not isinstance(text, Ok):
        return Err("the arguments are not canonical JSON")
    try:
        return Ok(input_model(name).model_validate_json(text.value, strict=True))
    except ValidationError as error:
        return Err(f"invalid arguments for {name}: {error.error_count()} error(s): {error}")
