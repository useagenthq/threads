"""spec/api.json `TeamAgent`: an agent defined with a team, whose runs also return the team."""

import asyncio
from typing import Final, TypedDict, Unpack, overload

from threads.agents.agent import Agent, Emit
from threads.agents.results import StreamEvent
from threads.agents.run import Input, RunOptions, RunOptionsWithDeps
from threads.agents.stream import RunStream
from threads.agents.team_results import TeamRunResult, with_team


class TeamLimits(TypedDict, total=False):
    """agent(team_limits=...): the team's limits."""

    concurrent: int
    """Most members running at once; a start past it is refused concurrency_cap. Default 4."""
    mailbox: int
    """Most pending mails per member; a send past it is refused mailbox_full. Default 100."""


class TeamRunStream[O](RunStream[O]):
    """spec/api.json `TeamRunStream`: a RunStream whose result is a TeamRunResult."""

    def __init__(
        self, queue: asyncio.Queue[StreamEvent | None], task: asyncio.Task[TeamRunResult[O]]
    ) -> None:
        super().__init__(queue, task)
        self._team_task: Final = task

    @property
    def result(self) -> asyncio.Task[TeamRunResult[O]]:
        """Await it for the `TeamRunResult`."""
        return self._team_task


class TeamAgent[D, O](Agent[D, O]):
    """spec/api.json `TeamAgent`: runs as `Agent.run` does, returning once the lead has reacted
    to every member this run started; its result also carries the team."""

    @overload
    async def run(
        self: "TeamAgent[None, O]", input: Input, **options: Unpack[RunOptions[None]]
    ) -> TeamRunResult[O]: ...
    @overload
    async def run(
        self, input: Input, **options: Unpack[RunOptionsWithDeps[D]]
    ) -> TeamRunResult[O]: ...
    async def run[T](
        self: "TeamAgent[T, O]", input: Input, **options: Unpack[RunOptions[T]]
    ) -> TeamRunResult[O]:
        """Runs one input to its run's end, and the team with it."""
        return await self._team_run(input, options, _drop)

    @overload
    def stream(
        self: "TeamAgent[None, O]", input: Input, **options: Unpack[RunOptions[None]]
    ) -> TeamRunStream[O]: ...
    @overload
    def stream(
        self, input: Input, **options: Unpack[RunOptionsWithDeps[D]]
    ) -> TeamRunStream[O]: ...
    def stream[T](
        self: "TeamAgent[T, O]", input: Input, **options: Unpack[RunOptions[T]]
    ) -> TeamRunStream[O]:
        """The same run as `run`, streamed. Call it inside a running event loop."""
        queue: asyncio.Queue[StreamEvent | None] = asyncio.Queue()
        run = self._team_run(input, options, queue.put_nowait)
        return TeamRunStream(queue, asyncio.get_running_loop().create_task(run))

    async def _team_run(self, input: Input, options: RunOptions[D], emit: Emit) -> TeamRunResult[O]:
        return await with_team(await self._run(input, options, self._deps(options), emit))


def _drop(_item: StreamEvent) -> None:
    pass
