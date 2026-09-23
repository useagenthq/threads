# threads

**Stop rebuilding the same agent plumbing, and turn every run into a replayable, forkable test.**

Teams building agents keep rebuilding the same pieces: a sandbox, a Slack bot, a WhatsApp bot, hooks, a knowledge base, memory, evals. threads brings those pieces into one framework for **TypeScript and Python**. You write what your agent does and connect your accounts.

> **Status: alpha, not yet published.** Every feature below is built in both languages and covered by tests, but the packages aren't on npm or PyPI yet and the APIs may still change. Install from source (below) to try it.

## What you get

- **Sandboxes:** your agent's code and tools run in E2B, Daytona or Modal. Snapshots let you fork a run with its files restored.
- **Channels:** the same agent in Slack, WhatsApp and GitHub, through an optional host with a typed HTTP API.
- **Knowledge base and memory:** search your docs and remember facts per user or repo, never mixed between tenants. Local SQLite built in; Supermemory and Zep adapters.
- **Hooks, permissions and approvals:** allow, block or ask a person before a risky action; every decision is recorded.
- **Tools, MCP, skills and subagents:** typed tools, any MCP server in one line, skills loaded on demand, helper agents, handoffs and teams.
- **Built-in tools:** shell, files, search, web fetch and search, git, code intelligence, notebooks and computer use.
- **Timeline, fork and tests:** every step is recorded. Go back to a saved step, restore the sandbox, try again, and keep the run as a CI test.

## What it looks like

```ts
import { agent, tool } from "@threads/core";
import { anthropic } from "@threads/anthropic";
import { e2b } from "@threads/e2b";
import { z } from "zod";

const openPullRequest = tool({
  name: "open_pull_request",
  description: "Open a pull request with the fix.",
  input: z.object({ title: z.string(), branch: z.string() }),
  runs: "host",
  execute: async ({ title, branch }) => myForge.openPr(title, branch),
});

const fixer = agent({
  name: "fixer",
  instructions: "Fix failing CI and open a pull request.",
  model: anthropic({                // ANTHROPIC_API_KEY
    model: "claude-sonnet-5",
    maxTokens: 8192,
    contextWindow: 200_000,
    maxOutputTokens: 8192,
  }),
  sandbox: e2b(),                   // E2B_API_KEY
  tools: [openPullRequest],
});

const result = await fixer.run("CI is failing on main");
```

```python
from pydantic import BaseModel
from threads import agent, tool
from threads.anthropic import anthropic
from threads.e2b import e2b

class PrInput(BaseModel):
    title: str
    branch: str

async def open_pr(args: PrInput, ctx) -> str:
    return await my_forge.open_pr(args.title, args.branch)

open_pull_request = tool(
    name="open_pull_request",
    description="Open a pull request with the fix.",
    input=PrInput,
    runs="host",
    execute=open_pr,
)

fixer = agent(
    name="fixer",
    instructions="Fix failing CI and open a pull request.",
    model=anthropic(                  # ANTHROPIC_API_KEY
        "claude-sonnet-5",
        context_window=200_000,
        max_output_tokens=8192,
    ),
    sandbox=e2b(),                    # E2B_API_KEY
    tools=[open_pull_request],
)

result = await fixer.run("CI is failing on main")
```

The core is a plain library: no server needed. Channels, schedules and the HTTP API run in the optional host (`threads dev`).

## Providers

| | TypeScript | Python |
|---|---|---|
| Models | Anthropic, OpenAI, AI SDK bridge | Anthropic, OpenAI, LiteLLM (`openai/` route) |
| Sandboxes | E2B (Bun), Daytona | E2B, Daytona, Modal |
| Channels | Slack, WhatsApp, GitHub | Slack, WhatsApp, GitHub |
| Memory | local, Supermemory, Zep | local, Supermemory, Zep |

A provider is only supported when threads can check ownership on its real network calls; one that can't be checked is refused at setup rather than half-supported.

## Three rules it's built on

1. **Every step is recorded** in an append-only log: what the agent saw, decided and did. State is rebuilt from the log.
2. **No blind retries.** If an action's outcome is uncertain after a crash, threads checks before retrying, or pauses and asks you. Only one process may act on a thread at a time.
3. **Isolated test runs.** Forks and saved tests run in a separate sandbox, with outside actions blocked or replayed from the recording.

## Install from source

```sh
git clone https://github.com/useagenthq/threads && cd threads
./scripts/generate.sh              # builds the generated code from spec/

cd typescript && bun install && bun run check    # TypeScript: typecheck, lint, tests
cd ../python && uv sync --all-extras && uv run pytest   # Python
```

Requirements: [Bun](https://bun.sh) for TypeScript, [uv](https://docs.astral.sh/uv/) and Python 3.12+ for Python.

## Repository layout

| Folder | What's in it |
|---|---|
| `spec/` | The shared contract both languages follow: event log schema, public API map, SQLite layout, tool catalog and the conformance cases both implementations must pass |
| `typescript/` | The TypeScript framework (`@threads/*` packages) |
| `python/` | The Python framework (`threads`, with optional extras per provider) |
| `scripts/` | `generate.sh` (rebuild generated code) and repo checks |
| `AGENTS.md` | Coding rules for contributors and coding agents |

## Contributing

Read [`AGENTS.md`](AGENTS.md) first. A feature starts in `spec/`, lands in both languages, and passes the same conformance cases in each.

## License

[Apache-2.0](LICENSE)
