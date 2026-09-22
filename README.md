# threads

**Stop rebuilding the same agent plumbing, and turn every run into a replayable, forkable test.**

Teams building agents keep rebuilding the same pieces: a sandbox, a Slack bot, a WhatsApp bot, hooks, a knowledge base, memory, evals. threads brings those pieces into one framework for **TypeScript and Python**. You write what your agent does and connect your accounts.

> **Status:** pre-alpha. The design is done; the code is being written. Nothing to install yet.

## What you get

- **Sandboxes:** your agent's code and tools run in E2B, Daytona or Modal.
- **Channels:** the same agent in Slack, WhatsApp and GitHub, through an optional host.
- **Knowledge base and memory:** search your docs; remember per user or repo, never mixed between customers.
- **Hooks and approvals:** allow, block or ask a human before risky actions.
- **Tools, MCP and subagents.**
- **Timeline, fork and tests:** every step is recorded. Go back to a saved step, restore the sandbox, try again, and keep the case as a CI test.

## What it looks like (planned API)

```ts
import { agent } from "@threads/core";
import { anthropic } from "@threads/anthropic";
import { e2b } from "@threads/e2b";

const fixer = agent({
  instructions: "Fix failing CI and open a pull request.",
  model: anthropic("your-model"),
  sandbox: e2b({ template: "node" }),
  tools: [openPullRequest],
});

const result = await fixer.run("CI is failing on main");
```

```python
from threads import agent
from threads.anthropic import anthropic
from threads.e2b import e2b

fixer = agent(
    instructions="Fix failing CI and open a pull request.",
    model=anthropic("your-model"),
    sandbox=e2b(template="python"),
    tools=[open_pull_request],
)

result = await fixer.run("CI is failing on main")
```

The core is a plain library: no server needed. Channels and schedules run in the optional host (`threads dev`).

## Three rules it's built on

1. **Every step is recorded** in an append-only log: what the agent saw, decided and did.
2. **No blind retries.** If an action's outcome is uncertain after a crash, threads checks before retrying, or pauses and asks you.
3. **Isolated test runs.** Forks and saved tests run in a separate sandbox, with outside actions blocked or simulated.

## Repository layout

| Folder | What's in it |
|---|---|
| `spec/` | The shared contract both languages follow: the event log schema and conformance tests |
| `typescript/` | The TypeScript framework (npm) |
| `python/` | The Python framework (PyPI) |
| `AGENTS.md` | Coding rules for contributors and coding agents |
