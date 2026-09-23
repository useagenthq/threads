"""The Daytona API and toolbox responses this adapter reads, parsed at the boundary."""

from pydantic import Field

from threads.adapters.sandboxes.wire import Wire


class SandboxDto(Wire):
    id: str = Field(min_length=1)
    name: str
    state: str | None = None
    toolbox_proxy_url: str = Field(alias="toolboxProxyUrl", min_length=1)


class SnapshotDto(Wire):
    id: str = Field(min_length=1)
    name: str
    state: str


class Executed(Wire):
    cmd_id: str = Field(alias="cmdId", min_length=1)


class Command(Wire):
    exit_code: int | None = Field(default=None, alias="exitCode")
