"""The git gateway against a local bare repository as the forge and real
local processes as the sandbox: clone and fetch hand the sandbox bundles, push goes out from
the host and reconciles by the forge's branch, and the credential canary never enters the
sandbox (invariant 4). Pull requests run against a scripted forge API."""

import asyncio
import subprocess
from collections.abc import Awaitable, Callable
from pathlib import Path

import pytest
from local_sandbox import LocalSandbox, LocalSession
from pydantic import JsonValue
from sandbox_kit import KitContext

from threads import Agent, Completed, EventItem, Thread, agent, scripted_model, sqlite
from threads._generated.tools_v1 import GitPushInput
from threads.git.forge import GitHub
from threads.git.gateway import Forge, GitGateway
from threads.log import (
    CallId,
    EffectBeginEvent,
    EffectResolvedEvent,
    Event,
    JsonObject,
    Permissions,
    Spill,
    ToolResultEvent,
)
from threads.loop.model import Found, NotFound, NotFoundNonfinal
from threads.loop.tools import Dispatched, Invocation, NotSent, Output, Uncertain
from threads.result import Err, Ok
from threads.secrets import secret
from threads.store import SqliteStore
from threads.tools import SandboxTools, specs
from threads.tools.specs import GIT
from threads.web.guard import Target
from threads.web.http import Fence, Request, Response, WebError

CANARY = "ghs_canary_token_do_not_leak"
LIMITS = Spill(threshold_bytes=4096, head_bytes=2048, tail_bytes=2048, request_budget_bytes=8192)
SPECS = {s.name: s for s in specs(sandbox=True, egress_denied=True, gated=GIT)}
ID = ["-c", "user.email=t@example.com", "-c", "user.name=T"]


def sh(*args: str, cwd: Path) -> str:
    done = subprocess.run(["git", *ID, *args], cwd=cwd, check=True, capture_output=True, text=True)  # noqa: S603, S607 - test setup
    return done.stdout.strip()


def make_forge(root: Path) -> Path:
    forge = root / "forge"
    (forge / "acme").mkdir(parents=True)
    sh("init", "--bare", "--quiet", "-b", "main", "acme/app.git", cwd=forge)
    work = root / "seed"
    work.mkdir()
    sh("init", "--quiet", "-b", "main", cwd=work)
    (work / "README.md").write_text("hello\n")
    sh("add", ".", cwd=work)
    sh("commit", "--quiet", "-m", "first", cwd=work)
    sh("push", "--quiet", str(forge / "acme/app.git"), "main", cwd=work)
    return forge


async def _open() -> bool:
    return True


async def _closed() -> bool:
    return False


type Body = Callable[[GitGateway, Callable[[Fence], GitGateway], Path, Path], Awaitable[None]]


def run(tmp_path: Path, body: Body, api: GitHub | None = None) -> None:
    forge = make_forge(tmp_path)
    workspace = tmp_path / "ws"
    workspace.mkdir()

    async def main() -> None:
        opened = await SqliteStore.open()
        assert isinstance(opened, Ok)
        session = LocalSession(workspace)
        tools = SandboxTools(session.open, KitContext(), opened.value, lambda: LIMITS)
        where = Forge(lambda repo: str(forge / f"{repo}.git"), api or GitHub())

        def gateway(fence: Fence) -> GitGateway:
            return GitGateway(CANARY, tools, fence, where)

        await body(gateway(_open), gateway, workspace, forge)
        await opened.value.close()

    asyncio.run(main())


def call(name: str, input: JsonObject, key: str = "b:call_1") -> Invocation:
    return Invocation(SPECS[name], CallId("call_1"), input, key)


def _no_canary(root: Path) -> None:
    for path in root.rglob("*"):
        if path.is_file():
            assert CANARY.encode() not in path.read_bytes(), path


def test_clone_push_and_reconcile_keep_the_credential_on_the_host(tmp_path: Path) -> None:
    async def body(
        gw: GitGateway, make: Callable[[Fence], GitGateway], ws: Path, forge: Path
    ) -> None:
        got = await gw.dispatch(call("git_clone", {"repo": "acme/app"}))
        assert isinstance(got, Output)
        assert got.text.startswith("cloned acme/app into /workspace/app at "), got.text
        assert (ws / "app/README.md").read_text() == "hello\n"
        assert sh("remote", "get-url", "origin", cwd=ws / "app") == str(forge / "acme/app.git")
        assert not list((ws / ".threads").iterdir())
        # The agent commits in the sandbox; the gateway pushes from the host.
        sh("checkout", "--quiet", "-b", "feature", cwd=ws / "app")
        (ws / "app/new.txt").write_text("x\n")
        sh("add", ".", cwd=ws / "app")
        sh("commit", "--quiet", "-m", "feature", cwd=ws / "app")
        head = sh("rev-parse", "HEAD", cwd=ws / "app")
        push = call("git_push", {"repo": "acme/app", "branch": "feature"}, "b:call_2")
        stale = await make(_closed).dispatch(push)
        assert isinstance(stale, NotSent)
        assert sh("branch", "--list", "feature", cwd=forge / "acme/app.git") == ""
        pushed = await gw.dispatch(push)
        assert pushed == Output(f"pushed {head} to acme/app feature")
        assert sh("rev-parse", "refs/heads/feature", cwd=forge / "acme/app.git") == head
        # A push whose answer was lost is found by the forge's branch.
        assert await gw.lookup(push) == Found(f"pushed {head} to acme/app feature")
        _no_canary(ws)

    run(tmp_path, body)


def test_fetch_brings_new_branches_and_a_moved_branch_is_unknown(tmp_path: Path) -> None:
    async def body(
        gw: GitGateway, _make: Callable[[Fence], GitGateway], ws: Path, forge: Path
    ) -> None:
        await gw.dispatch(call("git_clone", {"repo": "acme/app", "path": "src"}))
        other = forge.parent / "other"
        sh("clone", "--quiet", str(forge / "acme/app.git"), str(other), cwd=forge.parent)
        sh("checkout", "--quiet", "-b", "topic", cwd=other)
        sh("commit", "--quiet", "--allow-empty", "-m", "topic", cwd=other)
        sh("push", "--quiet", "origin", "topic", cwd=other)
        got = await gw.dispatch(call("git_fetch", {"repo": "acme/app", "path": "src"}, "b:c2"))
        assert got == Output("fetched acme/app into /workspace/src: refs/remotes/origin/*")
        assert sh("rev-parse", "origin/topic", cwd=ws / "src") == sh("rev-parse", "HEAD", cwd=other)
        # The local branch isn't where the forge's is: not found, but not finally (it parks).
        push = call("git_push", {"repo": "acme/app", "branch": "main", "path": "src"})
        sh("commit", "--quiet", "--allow-empty", "-m", "local", cwd=ws / "src")
        assert await gw.lookup(push) == NotFoundNonfinal()
        rejected = await gw.dispatch(
            call("git_push", {"repo": "acme/app", "branch": "nope", "path": "src"})
        )
        assert isinstance(rejected, Output)
        assert rejected.is_error

    run(tmp_path, body)


class _Api:
    """A scripted forge API: GET lists pulls, POST creates one."""

    def __init__(self, listed: bytes, created: Response | None = None) -> None:
        self.listed = listed
        self.created = created
        self.requests: list[tuple[str, Request]] = []

    async def send(
        self, target: Target, request: Request, fence: Fence, max_bytes: int
    ) -> Ok[Response] | Err[WebError]:
        if not await fence():
            return Err(WebError("stale_epoch", "lost", sent=False))
        self.requests.append((target.path, request))
        if request.method == "GET":
            return Ok(Response(200, {}, self.listed))
        if self.created is None:
            return Err(WebError("unavailable", "reset", sent=True))
        return Ok(self.created)


async def _public(_host: str, _port: int) -> list[str]:
    return ["140.82.112.6"]


def test_open_pull_request_returns_an_existing_one_and_reconciles_by_head(tmp_path: Path) -> None:
    pr = b'{"number": 7, "html_url": "https://github.com/acme/app/pull/7"}'
    existing = _Api(b"[" + pr + b"]")
    fresh = _Api(b"[]", Response(201, {}, pr))
    lost = _Api(b"[]")
    args: JsonObject = {"repo": "acme/app", "head": "feature", "base": "main", "title": "T"}
    for api, want in (
        (
            existing,
            Output(
                "pull request #7: https://github.com/acme/app/pull/7 (already open for feature)"
            ),
        ),
        (fresh, Output("pull request #7: https://github.com/acme/app/pull/7")),
        (lost, Uncertain("transport_error")),
    ):

        async def body(
            gw: GitGateway, *_rest: object, api: _Api = api, want: object = want
        ) -> None:
            got = await gw.dispatch(call("open_pull_request", args))
            assert got == want
            path, request = api.requests[0]
            assert path == "/repos/acme/app/pulls?state=open&head=acme%3Afeature"
            assert request.headers["Authorization"] == f"Bearer {CANARY}"
            if api is lost:
                assert await gw.lookup(call("open_pull_request", args)) == NotFound()

        run(tmp_path / str(id(api)), body, GitHub("https://api.github.com", api, _public))


class _CrashError(Exception):
    pass


_USAGE: JsonValue = {"input_tokens": 1, "output_tokens": 1}
_DONE: JsonValue = {
    "content": [{"type": "text", "text": "ok"}],
    "stop_reason": "end_turn",
    "usage": _USAGE,
}
_BYPASS = Permissions(
    mode="bypass",
    allow=[],
    ask=[],
    deny=[],
    protected_paths=[],
    allow_bypass=True,
    plan_exit_mode="default",
)


def _use(name: str, args: JsonValue, call_id: str) -> JsonValue:
    part: JsonValue = {"type": "tool_use", "call_id": call_id, "name": name, "input": args}
    return {"content": [part], "stop_reason": "tool_use", "usage": _USAGE}


def test_a_push_interrupted_by_a_crash_is_settled_by_the_forge_branch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A crash after the push reached the forge: the next run's recovery looks it up by the
    forge's branch, settles it confirmed_success and never pushes again (F11.12)."""
    forge = make_forge(tmp_path)
    ws = tmp_path / "ws"
    ws.mkdir()
    box = LocalSandbox(ws)
    monkeypatch.setenv("GH_TOKEN", CANARY)
    local = Forge(lambda repo: str(forge / f"{repo}.git"))
    monkeypatch.setattr("threads.agents.catalog.Forge", lambda: local)
    who = " ".join(ID)
    commit = f"cd app && git checkout -q -b feature && git {who} commit -q --allow-empty -m f"
    first = [
        _use("git_clone", {"repo": "acme/app"}, "c1"),
        _use("bash", {"command": commit}, "c2"),
        _use("git_push", {"repo": "acme/app", "branch": "feature"}, "c3"),
    ]
    pushed = GitGateway._push_bundle  # pyright: ignore[reportPrivateUsage] - crash after the real push

    async def crash(self: GitGateway, args: GitPushInput, bundle: bytes, sha: str) -> Dispatched:
        await pushed(self, args, bundle, sha)
        raise _CrashError

    def bot(responses: list[JsonValue]) -> Agent[None]:
        return agent(
            model=scripted_model({"responses": responses}),
            sandbox=box,
            git={"credential": secret("GH_TOKEN")},
            permissions=_BYPASS,
        )

    async def main() -> list[Event]:
        store = sqlite(str(tmp_path / "db" / "threads.db"))
        monkeypatch.setattr(GitGateway, "_push_bundle", crash)
        stream = bot(first).stream("push it", store=store)
        seen = [i.event async for i in stream if isinstance(i, EventItem)]
        with pytest.raises(_CrashError):
            await stream.result
        monkeypatch.setattr(GitGateway, "_push_bundle", pushed)
        thread = Thread(seen[0].thread_id, seen[0].branch_id, store)
        done = await bot([_DONE, _DONE]).run("status?", store=store, thread=thread)
        assert isinstance(done, Completed), done
        timeline = await done.thread.timeline()
        assert isinstance(timeline, Ok)
        return [e.event for e in timeline.value.entries]

    events = asyncio.run(main())
    head = sh("rev-parse", "refs/heads/feature", cwd=forge / "acme/app.git")
    settled = [e for e in events if isinstance(e, EffectResolvedEvent)]
    assert [(s.data.call_id, s.data.outcome) for s in settled] == [("c3", "confirmed_success")]
    begins = [e for e in events if isinstance(e, EffectBeginEvent) and e.data.call_id == "c3"]
    assert len(begins) == 1
    result = next(e for e in events if isinstance(e, ToolResultEvent) and e.data.call_id == "c3")
    assert result.data.preview == f"pushed {head} to acme/app feature"
