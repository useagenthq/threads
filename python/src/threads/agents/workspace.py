"""agent(workspace=...) resolved on the host, once per pin (spec/schema/README.md, Workspace
inputs). It runs after setup, so every secret the agent resolves is registered and an input file
holding one is refused."""

from dataclasses import replace

from threads.agents.definition import Definition
from threads.sandbox.tree.tree import Tree
from threads.secrets import resolve
from threads.workspace import Forge, Keep, resolve_workspace


async def no_keep(_data: bytes) -> None:
    """check() resolves the inputs to prove they read and to pin them; it stores nothing."""
    return None


async def with_workspace[D](
    definition: Definition[D], keep: Keep = no_keep, *, child: bool = False
) -> tuple[Definition[D], Tree | None]:
    """The definition with policy.workspace resolved, and the tree every sandbox it creates
    starts from. Unchanged when the agent has no workspace, or is a subagent or handoff target,
    which runs in its parent's sandbox and pins no workspace. Raises ConfigError."""
    ws = definition.workspace
    if ws is None or definition.sandbox is None or child:
        return definition, None
    catalog = definition.catalog
    token = None if catalog.git is None else resolve(catalog.git)
    forge = Forge(catalog.forge.git_url, token)
    resolved = await resolve_workspace(ws, forge, keep)
    return replace(definition, workspace_pin=resolved.pin), resolved.tree
