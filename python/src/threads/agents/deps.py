"""Whether an agent's runs may omit deps: every app tool, extension tool and hook annotates its
context `RunContext[None]` (spec/api.json Agent.run deps: "omitted, tools see null"). Read from
the annotations, which are their static types."""

from typing import TypeGuard

from threads._tool_deps import ContextDeps, context_deps
from threads.agents.definition import Definition
from threads.agents.tool import Tool


def readers[D](definition: Definition[D]) -> list[tuple[str, ContextDeps]]:
    """Each app tool, extension tool and hook, named for an error, with what its context reads."""
    out: list[tuple[str, ContextDeps]] = [(f"tool {t.name}", _tool(t)) for t in definition.tools]
    for e in definition.extensions:
        out.extend((f"tool {e.name}__{t.name}", _tool(t)) for t in e.tools)
        out.extend((f"hook {e.name}.{h}", context_deps(fn, -1)) for h, fn in e.hooks.items())
    return out


def needs_no_deps[D](definition: Definition[D]) -> TypeGuard[Definition[None]]:
    return all(deps == "none" for _, deps in readers(definition))


def _tool(app_tool: object) -> ContextDeps:
    """A `tool()`'s context annotation; another AppTool's is never read, so it needs deps."""
    # Read before narrowing: only the callable matters, not the Tool's type parameters.
    execute: object = getattr(app_tool, "execute", None)
    return context_deps(execute) if isinstance(app_tool, Tool) else "deps"
