"""Every Docker Engine answer, and everything the container writes, parsed at the boundary
(AGENTS.md, Trust boundaries). Known fields are strict; fields the daemon adds are ignored.
A parse failure is a typed `unavailable`, never a cast.

The container's own bytes (a record, `supervise --check`) are as untrusted as the daemon's:
uid 1000 can't write them, but nothing here assumes that.
"""

from typing import Literal

from pydantic import Field, TypeAdapter

from threads.adapters.sandboxes.wire import Wire


class Failure(Wire):
    """Every error body the daemon sends: `{"message": "..."}` and nothing else."""

    message: str


class Created(Wire):
    """POST /containers/create."""

    id: str = Field(alias="Id", min_length=1)
    warnings: tuple[str, ...] = Field(default=(), alias="Warnings")


class Limits(Wire):
    """The limits the daemon actually applied, from a container inspect."""

    nano_cpus: int = Field(default=0, alias="NanoCpus")
    memory: int = Field(default=0, alias="Memory")
    pids_limit: int = Field(default=0, alias="PidsLimit")


class ContainerState(Wire):
    running: bool = Field(alias="Running")
    status: str = Field(default="", alias="Status")


class Inspected(Wire):
    """GET /containers/<id>/json."""

    id: str = Field(alias="Id", min_length=1)
    state: ContainerState = Field(alias="State")
    host_config: Limits = Field(default_factory=Limits, alias="HostConfig")


class Image(Wire):
    """GET /images/<ref>/json: which supervisor binary the container gets."""

    architecture: str = Field(alias="Architecture", min_length=1)


class Listed(Wire):
    """One row of GET /containers/json."""

    id: str = Field(alias="Id", min_length=1)
    names: tuple[str, ...] = Field(default=(), alias="Names")


class ExecMade(Wire):
    """POST /containers/<id>/exec."""

    id: str = Field(alias="Id", min_length=1)


class ExecState(Wire):
    """GET /exec/<id>/json."""

    running: bool = Field(alias="Running")
    exit_code: int | None = Field(default=None, alias="ExitCode")


class PullLine(Wire):
    """One line of the streamed POST /images/create body."""

    error: str | None = None


class Tools(Wire):
    """`supervise --check` on stdout: what the image carries."""

    sh: bool
    env: bool
    bash: bool
    tar: bool
    git: bool
    python3: bool


type RecordState = Literal["running", "exited", "terminated", "stuck"]


class Record(Wire):
    """One `state/records/<key-hash>.json`, written by the supervisor."""

    key: str = Field(min_length=1)
    generation: str = Field(min_length=1)
    state: RecordState
    child_pid: int
    supervisor_pid: int
    supervisor_start: int
    deadline_ms: int
    exit_code: int


LISTED: TypeAdapter[tuple[Listed, ...]] = TypeAdapter(tuple[Listed, ...])
