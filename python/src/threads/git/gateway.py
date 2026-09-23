"""The git gateway as a tool runner (invariant 4). The forge credential stays on
the host: clone and fetch run on the host and hand the sandbox a git bundle, and push takes a
bundle of the branch out of the sandbox and pushes it from the host. The sandbox's remote URL
has no credential. Push and open_pull_request are reconcilable: a push is found when the
forge's branch is at the pushed commit, a pull request by its head branch."""

import re
import tempfile
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Final

from pydantic import BaseModel
from pydantic.experimental.missing_sentinel import MISSING

from threads._generated.tools_v1 import (
    GitCloneInput,
    GitFetchInput,
    GitPushInput,
    OpenPullRequestInput,
)
from threads.git.forge import GitHub, Refused
from threads.git.host import credential_env, git
from threads.log import JsonObject, ToolSpec
from threads.log.digest import sha256_hex
from threads.loop.model import Found, LookupResult, LookupUnknown, NotFound, NotFoundNonfinal
from threads.loop.tools import Dispatched, Invocation, NotSent, Output, Termination, Uncertain
from threads.result import Err, Ok
from threads.tools import files
from threads.tools.runner import SandboxTools, parse
from threads.web.http import Fence, WebError

STAGING: Final = "/workspace/.threads"
"""Where bundles pass through the sandbox; a protected path, so no model tool writes there."""


def github_url(repo: str) -> str:
    return f"https://github.com/{repo}.git"


@dataclass(frozen=True, slots=True)
class Forge:
    """Where repositories live: git remotes by `owner/name`, and the pull request API."""

    git_url: Callable[[str], str] = github_url
    api: GitHub = field(default_factory=GitHub)


_REF: Final = re.compile(r"[A-Za-z0-9._/-]+")


def _safe_ref(ref: str) -> bool:
    """A plain branch, tag or commit name: never an option, a range or a lock file."""
    return (
        _REF.fullmatch(ref) is not None
        and not ref.startswith("-")
        and ".." not in ref
        and not ref.endswith((".lock", "/"))
    )


def _where(repo: str, path: str | MISSING, *refs: str | MISSING) -> Ok[str] | Err[Output]:
    """The clone's directory, which must stay inside /workspace, once every ref is safe."""
    bad = next((r for r in refs if r is not MISSING and not _safe_ref(r)), None)
    if bad is not None:
        return Err(Output(f"ref {bad} is not a plain branch, tag or commit name", True))
    where = files.absolute(repo.split("/", 1)[-1] if path is MISSING else path)
    if not where.startswith(f"{files.WORKSPACE}/"):
        return Err(Output("path must stay inside /workspace", True))
    return Ok(where)


class GitGateway:
    def __init__(self, token: str, sandbox: SandboxTools, fence: Fence, forge: Forge) -> None:
        self._token = token
        self._sandbox = sandbox
        self._fence = fence
        self._forge = forge

    def invalid(self, spec: ToolSpec, input: JsonObject) -> str | None:
        parsed = parse(spec.name, input)
        return parsed.error if isinstance(parsed, Err) else None

    def _args(self, call: Invocation) -> BaseModel:
        parsed = parse(call.spec.name, call.input)
        if isinstance(parsed, Err):
            raise AssertionError(f"a dispatched {call.spec.name} call was parsed first")
        return parsed.value

    async def dispatch(self, call: Invocation) -> Dispatched:
        match self._args(call):
            case GitCloneInput() as args:
                return await self._clone(args, call.effect_key)
            case GitFetchInput() as args:
                return await self._fetch(args, call.effect_key)
            case GitPushInput() as args:
                return await self._push(args, call.effect_key)
            case OpenPullRequestInput() as args:
                return await self._pull_request(args)
            case other:
                raise AssertionError(f"no git gateway tool takes {type(other).__name__}")

    def _env(self, home: Path) -> Mapping[str, str]:
        return credential_env(self._token, home)

    async def _in_sandbox(self, script: str, key: str, *args: str) -> Ok[str] | Err[Dispatched]:
        """A control script in the sandbox; its stdout, or the outcome standing for it."""
        ran = await self._sandbox.command(["bash", "-c", script, "gateway", *args], key)
        if isinstance(ran, Err):
            return ran
        if ran.value.exit_code != 0:
            return Err(Output(f"git failed in the sandbox: {ran.value.stderr.strip()}", True))
        return Ok(ran.value.stdout.strip())

    async def _bundle_from_forge(self, repo: str, key: str) -> Ok[str] | Err[Dispatched]:
        """A bundle of every branch and tag of the repo, staged in the sandbox."""
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            if not await self._fence():
                return Err(NotSent())
            url = self._forge.git_url(repo)
            cloned = await git(
                ["clone", "--bare", "--quiet", url, "repo.git"], home, self._env(home)
            )
            if cloned.exit_code != 0:
                return Err(
                    Output(f"unavailable: cloning {repo} failed: {cloned.stderr.strip()}", True)
                )
            made = await git(
                ["bundle", "create", "../out.bundle", "--all"], home / "repo.git", self._env(home)
            )
            if made.exit_code != 0:
                return Err(
                    Output(f"unavailable: bundling {repo} failed: {made.stderr.strip()}", True)
                )
            data = (home / "out.bundle").read_bytes()
        session = await self._sandbox.session()
        if session is None:
            return Err(NotSent())
        staged = f"{STAGING}/{sha256_hex(key.encode())[:16]}.bundle"
        up = await session.upload(staged, data, self._sandbox.context)
        return (
            Err(Output(f"{up.error.code}: {up.error.message}", True))
            if isinstance(up, Err)
            else Ok(staged)
        )

    async def _clone(self, args: GitCloneInput, key: str) -> Dispatched:
        where = _where(args.repo, args.path, args.ref)
        if isinstance(where, Err):
            return where.error
        path = where.value
        staged = await self._bundle_from_forge(args.repo, key)
        if isinstance(staged, Err):
            return staged.error
        ref = "" if args.ref is MISSING else args.ref
        script = (
            'git clone --quiet "$1" "$2" && rm -f "$1" && cd "$2" && '
            'git remote set-url origin "$3" && { [ -z "$4" ] || git checkout --quiet "$4"; } && '
            "git rev-parse HEAD"
        )
        url = self._forge.git_url(args.repo)
        done = await self._in_sandbox(script, key, staged.value, path, url, ref)
        if isinstance(done, Err):
            return done.error
        return Output(f"cloned {args.repo} into {path} at {done.value}")

    async def _fetch(self, args: GitFetchInput, key: str) -> Dispatched:
        where = _where(args.repo, args.path, args.ref)
        if isinstance(where, Err):
            return where.error
        path = where.value
        staged = await self._bundle_from_forge(args.repo, key)
        if isinstance(staged, Err):
            return staged.error
        spec = "refs/heads/*" if args.ref is MISSING else f"refs/heads/{args.ref}"
        target = spec.replace("refs/heads/", "refs/remotes/origin/")
        script = (
            'cd "$2" && git fetch --quiet --tags "$1" "+$3:$4"; code=$?; rm -f "$1"; exit $code'
        )
        done = await self._in_sandbox(script, key, staged.value, path, spec, target)
        if isinstance(done, Err):
            return done.error
        return Output(f"fetched {args.repo} into {path}: {target}")

    async def _local_head(self, args: GitPushInput, key: str) -> Ok[str] | Err[Dispatched]:
        where = _where(args.repo, args.path, args.branch)
        if isinstance(where, Err):
            return where
        return await self._in_sandbox(
            'cd "$1" && git rev-parse --verify "refs/heads/$2^{commit}"',
            key,
            where.value,
            args.branch,
        )

    async def _push(self, args: GitPushInput, key: str) -> Dispatched:
        where = _where(args.repo, args.path, args.branch)
        if isinstance(where, Err):
            return where.error
        path = where.value
        staged = f"{STAGING}/{sha256_hex(key.encode())[:16]}.bundle"
        script = (
            'mkdir -p "$(dirname "$3")" && cd "$1" && '
            'git bundle create --quiet "$3" "refs/heads/$2" && git rev-parse "refs/heads/$2"'
        )
        head = await self._in_sandbox(script, key, path, args.branch, staged)
        if isinstance(head, Err):
            return head.error
        session = await self._sandbox.session()
        if session is None:
            return NotSent()
        got = await session.download(staged, self._sandbox.context)
        await self._in_sandbox('rm -f "$1"', key, staged)
        if isinstance(got, Err):
            return Output(f"{got.error.code}: {got.error.message}", True)
        return await self._push_bundle(args, got.value, head.value)

    async def _push_bundle(self, args: GitPushInput, bundle: bytes, sha: str) -> Dispatched:
        ref = f"refs/heads/{args.branch}"
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            env = self._env(home)
            (home / "in.bundle").write_bytes(bundle)
            await git(["init", "--bare", "--quiet", "repo.git"], home, env)
            repo = home / "repo.git"
            took = await git(["fetch", "--quiet", "../in.bundle", f"{ref}:{ref}"], repo, env)
            got = await git(["rev-parse", ref], repo, env)
            if took.exit_code != 0 or got.stdout.strip() != sha:
                return Output(f"the bundle of {args.branch} did not verify; nothing pushed", True)
            if not await self._fence():
                return NotSent()
            url = self._forge.git_url(args.repo)
            pushed = await git(["push", "--porcelain", url, f"{ref}:{ref}"], repo, env)
        if pushed.exit_code == 0:
            return Output(_pushed(args, sha))
        if any(line.startswith("!") for line in pushed.stdout.splitlines()):
            # The forge answered and refused this ref: nothing changed.
            return Output(f"push rejected: {pushed.stdout.strip()} {pushed.stderr.strip()}", True)
        return Uncertain("transport_error")

    async def _pull_request(self, args: OpenPullRequestInput) -> Dispatched:
        api = self._forge.api
        found = await api.find(args.repo, args.head, args.base, self._token, self._fence)
        if isinstance(found, Err):
            return _failed(found.error)
        if found.value is not None:
            return Output(found.value.text())
        body = "" if args.body is MISSING else args.body
        made = await api.open(
            args.repo,
            head=args.head,
            base=args.base,
            title=args.title,
            body=body,
            token=self._token,
            fence=self._fence,
        )
        return _failed(made.error) if isinstance(made, Err) else Output(made.value.text())

    async def lookup(self, call: Invocation) -> LookupResult[str]:
        match self._args(call):
            case GitPushInput() as args:
                return await self._pushed(args, call.effect_key)
            case OpenPullRequestInput() as args:
                return await self._pull_found(args)
            case _:
                return LookupUnknown(f"{call.spec.name} has no lookup")

    async def _pull_found(self, args: OpenPullRequestInput) -> LookupResult[str]:
        """Found for any pull request from the head into the base, in any state, so a lost
        create that was closed since is never opened twice. None means it never landed."""
        api = self._forge.api
        found = await api.find(args.repo, args.head, args.base, self._token, self._fence)
        if isinstance(found, Err):
            return LookupUnknown("the forge did not answer")
        return NotFound() if found.value is None else Found(found.value.text())

    async def _pushed(self, args: GitPushInput, key: str) -> LookupResult[str]:
        """Found when the forge's branch is at the local branch's commit. Anything else is a
        nonfinal not_found (the forge may lag, or the head moved since), so it parks: a push is
        never repeated blindly. The sandbox read runs under its own process key, apart from the
        push's commands."""
        local = await self._local_head(args, f"{key}:lookup")
        if isinstance(local, Err):
            return LookupUnknown("the sandbox branch is gone")
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            if not await self._fence():
                return LookupUnknown("this run no longer owns the branch")
            url = self._forge.git_url(args.repo)
            remote = await git(
                ["ls-remote", url, f"refs/heads/{args.branch}"], home, self._env(home)
            )
        if remote.exit_code != 0:
            return LookupUnknown(f"the forge did not list {args.branch}")
        at = remote.stdout.split("\t", 1)[0].strip()
        return Found(_pushed(args, at)) if at == local.value else NotFoundNonfinal()

    async def terminate(self, call: Invocation) -> Termination:
        return "unknown"

    def provider_now(self) -> int | None:
        return None


def _pushed(args: GitPushInput, sha: str) -> str:
    return f"pushed {sha} to {args.repo} {args.branch}"


def _failed(error: WebError | Refused) -> Dispatched:
    match error:
        case Refused(message=message):
            return Output(message, True)
        case WebError(code="stale_epoch") | WebError(sent=False):
            return NotSent()
        case WebError(code="timeout"):
            return Uncertain("timeout")
        case WebError():
            return Uncertain("transport_error")
