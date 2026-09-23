"""The E2B control-plane responses threads relies on, parsed at the boundary (the SDK's own
attrs parsing is not validation)."""

from typing import Annotated

from pydantic import Field, JsonValue, TypeAdapter

from threads.adapters.sandboxes.wire import Wire


class Sandbox(Wire):
    """A created or described sandbox: how to reach its envd."""

    sandbox_id: Annotated[str, Field(alias="sandboxID", min_length=1)]
    envd_version: Annotated[str, Field(alias="envdVersion")]
    envd_access_token: Annotated[str | None, Field(alias="envdAccessToken")] = None
    domain: str | None = None


class Listed(Wire):
    sandbox_id: Annotated[str, Field(alias="sandboxID", min_length=1)]
    metadata: dict[str, JsonValue] | None = None


class Snapshot(Wire):
    snapshot_id: Annotated[str, Field(alias="snapshotID", min_length=1)]
    names: list[str]


LISTED: TypeAdapter[list[Listed]] = TypeAdapter(list[Listed])
SNAPSHOTS: TypeAdapter[list[Snapshot]] = TypeAdapter(list[Snapshot])
