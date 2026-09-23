<div align="center">

<picture>
  <source media="(prefers-color-scheme: dark)" srcset=".github/assets/hero-dark.svg">
  <img alt="threads: Agents you can replay, fork and trust." src=".github/assets/hero-light.svg" width="100%">
</picture>

<h3>An agent framework for TypeScript and Python, built on an append-only event log.</h3>

<p>
  <a href="https://github.com/useagenthq/threads/actions/workflows/typescript.yml"><img src="https://github.com/useagenthq/threads/actions/workflows/typescript.yml/badge.svg" alt="TypeScript CI"></a>
  <a href="https://github.com/useagenthq/threads/actions/workflows/python.yml"><img src="https://github.com/useagenthq/threads/actions/workflows/python.yml/badge.svg" alt="Python CI"></a>
  <a href="LICENSE"><img src="https://img.shields.io/badge/license-Apache--2.0-2563EB" alt="License: Apache-2.0"></a>
  <img src="https://img.shields.io/badge/python-3.12%2B-2563EB?logo=python&amp;logoColor=white" alt="Python 3.12+">
  <a href="https://threadsai.dev"><img src="https://img.shields.io/badge/docs-threadsai.dev-2563EB" alt="Docs: threadsai.dev"></a>
  <img src="https://img.shields.io/badge/status-alpha-B45309" alt="Status: alpha">
</p>

<p>
  <a href="https://threadsai.dev/docs/quickstart"><b>Quickstart</b></a> ·
  <a href="https://threadsai.dev/docs"><b>Docs</b></a> ·
  <a href="https://threadsai.dev/docs/how-it-works"><b>How it works</b></a> ·
  <a href="https://threadsai.dev/docs/comparison"><b>Comparison</b></a>
</p>

</div>

Every run of a threads agent is written to a log as it happens. From that one record you can replay the run step by step, resume it after a crash without repeating a side effect, fork it from a past step, and save it as a test case. Agents run code in isolated sandboxes, and your keys never go in with them.

Sandboxes, Slack, WhatsApp and GitHub channels, memory, hooks and MCP come built in. You write what your agent does and add your API keys.

> [!NOTE]
> threads is alpha. It is not on npm or PyPI yet, and APIs may change. [Install from source](#install) to try it.

## Quick example

An agent that fixes failing tests in its own sandbox.

**TypeScript**

```ts
import { agent, sqlite } from "@threads/core";
import { anthropic } from "@threads/anthropic";
import { e2b } from "@threads/e2b";

const coder = agent({
  name: "coder",
  instructions: "Fix the failing tests in /workspace.",
  model: anthropic({
    model: "claude-sonnet-5",
    maxTokens: 8192,
    contextWindow: 1_000_000,
    maxOutputTokens: 128_000,
  }),
  sandbox: e2b(), // E2B_API_KEY. Your keys never enter the sandbox.
});

const result = await coder.run("Make the tests pass", { store: sqlite(".threads") });
console.log(result.status); // "completed", or "parked" until you approve a command
```

**Python**

```python
from threads import agent, sqlite
from threads.anthropic import anthropic
from threads.e2b import e2b

coder = agent(
    name="coder",
    instructions="Fix the failing tests in /workspace.",
    model=anthropic("claude-sonnet-5", context_window=1_000_000, max_output_tokens=128_000),
    sandbox=e2b(),  # E2B_API_KEY. Your keys never enter the sandbox.
)

result = await coder.run("Make the tests pass", store=sqlite(".threads"))
print(result.status)  # "completed", or "parked" until you approve a command
```

No API key yet? The [quickstart](https://threadsai.dev/docs/quickstart) runs the same kind of agent on a scripted model, offline.

## Why threads

<picture>
  <source media="(prefers-color-scheme: dark)" srcset=".github/assets/run-is-a-log-dark.svg">
  <img alt="Every run is a log: thread_started, user_input, model_request, model_response, tool_call, permission_decision, tool_result, turn_completed. Replay (timeline), resume (run again), fork (fork a point) and evals (saveCase) are all read from it." src=".github/assets/run-is-a-log-light.svg" width="100%">
</picture>

- **Replay any run.** `timeline()` walks through every step: each input, the exact request the model was sent, and every tool call and result. Debug a bad answer from production without adding logging first.
- **Crash-safe resume.** Run the same thread again and it continues from the log. A side effect that may already have happened is never silently repeated: threads proves what happened, or parks the run and asks you.
- **Fork from a past step.** `fork()` starts a new branch in a fresh sandbox restored from a snapshot. The original run is untouched.
- **Evals from real runs.** `saveCase()` turns a real turn into a regression case you commit next to your code.
- **An audit trail by default.** The log is append-only and hash-chained, so a changed or missing line is detected. Keys you pass with `secret()` never reach the log, a prompt or the sandbox.
- **One API, one log format, two languages.** TypeScript and Python follow one spec and write the same bytes. A thread written by one can be opened, inspected and forked by the other.

## Easy evals

<picture>
  <source media="(prefers-color-scheme: dark)" srcset=".github/assets/evals-flow-dark.svg">
  <img alt="A real run is recorded in the log. saveCase() writes cases/reads-notes/ with case.json, the TypeScript and Python logs, model.json and stubs.json. The turn replays offline with a scripted model built from the recorded replies, with no API keys and no network. A case runner is coming." src=".github/assets/evals-flow-light.svg" width="100%">
</picture>

When an agent gets something right, or you have just fixed something it got wrong, save that turn:

```ts
const saved = await thread.saveCase("reads-notes", {
  expect: { must: [{ type: "tool_result", data: { is_error: false } }] },
  externalEffects: "stub", // a case never makes real calls
});
```

The case holds the log up to that point, the model's replies, the recorded results of external calls and what the replay must produce. Replay it with a [scripted model](https://threadsai.dev/docs/evals/testing) and the in-memory `fakeSandbox()`: no API keys and no network. A built-in runner for saved cases is coming; today you replay them with the scripted model yourself. See [Saved cases](https://threadsai.dev/docs/evals/saved-cases).

## Sandbox-native

<picture>
  <source media="(prefers-color-scheme: dark)" srcset=".github/assets/sandbox-boundary-dark.svg">
  <img alt="Your process holds the agent loop, your tools, hooks and permissions, your API keys and a git gateway. It sends tool calls to the sandbox (E2B, Daytona or Modal), which runs bash and the file tools and sends back results. The sandbox has no credentials and no internet by default." src=".github/assets/sandbox-boundary-light.svg" width="100%">
</picture>

Set `sandbox` on an agent and it gets its own Linux machine with `bash`, file and search tools. The agent loop stays in your process and reaches the sandbox only through tools.

- **Your keys stay out.** Provider keys authenticate threads' own calls. Tools that need a credential, like `git_push` and `open_pull_request`, go through a gateway on your host.
- **No internet by default.** Opening the network takes two explicit settings, so it never happens by accident.
- **Forks get their own machine.** A fork restores the sandbox snapshot into a fresh sandbox. Daytona takes snapshots today; E2B and Modal don't yet.
- **Crashes never re-run a command blindly.** If the host dies mid-command, the run parks until you decide.

## Batteries included

<table>
  <tr>
    <td width="33%" valign="top"><b><a href="https://threadsai.dev/docs/evals/saved-cases">Evals and testing</a></b><br>Saved cases, a scripted model and a fake sandbox. Tests never call a real model.</td>
    <td width="33%" valign="top"><b><a href="https://threadsai.dev/docs/sandboxes/overview">Sandboxes</a></b><br>E2B, Daytona and Modal. No internet and no credentials by default.</td>
    <td width="33%" valign="top"><b><a href="https://threadsai.dev/docs/production/durability">Durability</a></b><br>Resume after a crash. Risky steps park for a human to decide.</td>
  </tr>
  <tr>
    <td valign="top"><b><a href="https://threadsai.dev/docs/host/overview">Channels</a></b><br>Slack, WhatsApp and GitHub, cron schedules and an HTTP API.</td>
    <td valign="top"><b><a href="https://threadsai.dev/docs/memory/memory">Memory and knowledge</a></b><br>Local memory, Supermemory or Zep, and a local knowledge base.</td>
    <td valign="top"><b><a href="https://threadsai.dev/docs/control/hooks">Hooks and permissions</a></b><br>Gate tools, redact results, allow / ask / deny rules, approvals and budgets.</td>
  </tr>
  <tr>
    <td valign="top"><b><a href="https://threadsai.dev/docs/agents/built-in-tools">Tools and MCP</a></b><br>Shell, files, search, web, git, code intelligence, notebooks, computer use, and any MCP server.</td>
    <td valign="top"><b><a href="https://threadsai.dev/docs/multi-agent/subagents">Subagents and handoffs</a></b><br>Let an agent start helpers or hand the conversation to a specialist.</td>
    <td valign="top"><b><a href="https://threadsai.dev/docs/evals/timeline">Timeline and fork</a></b><br>Inspect every step of a past run and branch it to try a fix.</td>
  </tr>
</table>

The core is a plain library: no server needed. Channels, schedules and the HTTP API run in the optional host (`threads dev`).

## Providers

| | TypeScript | Python |
|---|---|---|
| Models | Anthropic, OpenAI, AI SDK bridge | Anthropic, OpenAI, LiteLLM (`openai/` route) |
| Sandboxes | E2B (Bun), Daytona | E2B, Daytona, Modal |
| Channels | Slack, WhatsApp, GitHub | Slack, WhatsApp, GitHub |
| Memory | local, Supermemory, Zep | local, Supermemory, Zep |

Each provider is one line, and keys come from your environment unless you pass them. A provider is only supported when threads can check ownership on its real network calls; one that can't be checked is refused at setup rather than half-supported.

<details>
<summary><b>Models</b></summary>

```ts
import { anthropic } from "@threads/anthropic";
import { openai } from "@threads/openai";
import { aiSdk } from "@threads/ai-sdk";

model: anthropic({ model: "claude-sonnet-5", maxTokens: 8192, contextWindow: 1_000_000, maxOutputTokens: 128_000 }), // ANTHROPIC_API_KEY
model: openai({ model: "gpt-5.5", contextWindow: 1_050_000, maxOutputTokens: 128_000 }),                              // OPENAI_API_KEY
model: aiSdk({ model: (fetch) => yourProvider({ fetch })("model-id"), contextWindow: 128_000, maxOutputTokens: 8192 }), // any AI SDK provider
```

```python
from threads.anthropic import anthropic
from threads.litellm import litellm
from threads.openai import openai

model=anthropic("claude-sonnet-5", context_window=1_000_000, max_output_tokens=128_000)  # ANTHROPIC_API_KEY
model=openai("gpt-5.5", context_window=1_050_000, max_output_tokens=128_000)             # OPENAI_API_KEY
model=litellm("openai/my-model", base_url="http://localhost:4000", context_window=128_000, max_output_tokens=8192)
```

</details>

<details>
<summary><b>Sandboxes</b></summary>

```ts
import { e2b } from "@threads/e2b";
import { daytona } from "@threads/daytona";

sandbox: e2b({ template: "base" }),                          // E2B_API_KEY (runs on Bun)
sandbox: daytona(),                                          // DAYTONA_API_KEY
// Modal: Python only for now (its JS SDK's transport can't be fenced yet)
```

```python
from threads.daytona import daytona
from threads.e2b import e2b
from threads.modal import modal

sandbox=e2b(template="base")         # E2B_API_KEY
sandbox=daytona()                    # DAYTONA_API_KEY
sandbox=modal(image_id="im-...")     # MODAL_TOKEN_ID, MODAL_TOKEN_SECRET
```

To open the network, set the provider's option (`internet: true` for TS e2b, `network: "open"` for TS daytona, `allow_internet=True` in Python) **and** `egress: "unenforced"` on the agent.

</details>

<details>
<summary><b>Channels</b></summary>

Channels run in the optional host. Start it with `threads dev`; it prints each channel's webhook URL.

```ts
import { secret, sqlite } from "@threads/core";
import { host } from "@threads/host";
import { slack } from "@threads/slack";
import { whatsapp } from "@threads/whatsapp";
import { github } from "@threads/github";

export default host({
  store: sqlite(".threads"),
  agents: { fixer },
  channels: {
    slack: slack({ agent: "fixer", signingSecret: secret("SLACK_SIGNING_SECRET"), botToken: secret("SLACK_BOT_TOKEN") }),
    whatsapp: whatsapp({ agent: "fixer", appSecret: secret("WHATSAPP_APP_SECRET"), accessToken: secret("WHATSAPP_ACCESS_TOKEN") }),
    github: github({ agent: "fixer", webhookSecret: secret("GITHUB_WEBHOOK_SECRET"), token: secret("GITHUB_TOKEN") }),
  },
});
```

```python
from threads import secret, sqlite
from threads.github import github
from threads.host import host
from threads.slack import slack
from threads.whatsapp import whatsapp

app = host(
    store=sqlite(".threads"),
    agents={"fixer": fixer},
    channels={
        "slack": slack(agent="fixer", signing_secret=secret("SLACK_SIGNING_SECRET"), bot_token=secret("SLACK_BOT_TOKEN")),
        "whatsapp": whatsapp(
            agent="fixer",
            app_secret=secret("WHATSAPP_APP_SECRET"),
            access_token=secret("WHATSAPP_ACCESS_TOKEN"),
            verify_token=secret("WHATSAPP_VERIFY_TOKEN"),
            phone_number_id="1234567890",
        ),
        "github": github(agent="fixer", webhook_secret=secret("GITHUB_WEBHOOK_SECRET"), token=secret("GITHUB_TOKEN")),
    },
)
```

</details>

<details>
<summary><b>Memory, knowledge and MCP</b></summary>

```ts
import { localKnowledge, localMemory } from "@threads/core";
import { mcp } from "@threads/mcp";
import { supermemory } from "@threads/supermemory";
import { zep } from "@threads/zep";

memory: localMemory(),                                          // SQLite, in the run's store
memory: supermemory(),                                          // SUPERMEMORY_API_KEY
memory: zep(),                                                  // ZEP_API_KEY
knowledge: localKnowledge({ paths: ["./docs"] }),
tools: [mcp({ name: "docs", url: "https://example.com/mcp" })],
```

```python
from threads import local_knowledge, local_memory
from threads.mcp import mcp
from threads.supermemory import supermemory
from threads.zep import zep

memory=local_memory()                # SQLite, in the run's store
memory=supermemory()                 # SUPERMEMORY_API_KEY
memory=zep()                         # ZEP_API_KEY
knowledge=local_knowledge(paths=["./docs"])
tools=[mcp(name="docs", url="https://example.com/mcp")]
```

</details>

## Install

threads isn't on npm or PyPI yet, so run it from a clone:

```sh
git clone https://github.com/useagenthq/threads && cd threads
./scripts/generate.sh              # builds the generated code from spec/

cd typescript && bun install && bun run check    # TypeScript: typecheck, lint, tests
cd ../python && uv sync --all-extras && uv run pytest   # Python
```

You need [Bun](https://bun.sh) for TypeScript, and [uv](https://docs.astral.sh/uv/) with Python 3.12+ for Python. See [Installation](https://threadsai.dev/docs/installation).

## Coming next

Not built yet, so not claimed above:

- Packages on npm and PyPI
- A runner for saved cases
- Docker and local sandboxes; snapshots (and so forks) on E2B and Modal; Modal in TypeScript
- Network allowlists for sandboxes (today it is all blocked or all open)
- Agents messaging each other across threads, and the A2A protocol

## Documentation

- [**Quickstart**](https://threadsai.dev/docs/quickstart): run your first agent with no API key
- [**How it works**](https://threadsai.dev/docs/how-it-works): a short tour of the log behind every thread
- [**Durability**](https://threadsai.dev/docs/production/durability): what survives a crash, and what happens to each kind of tool
- [**Timeline**](https://threadsai.dev/docs/evals/timeline), [**Fork**](https://threadsai.dev/docs/evals/fork) and [**Saved cases**](https://threadsai.dev/docs/evals/saved-cases): inspect, branch and test real runs
- [**Comparison**](https://threadsai.dev/docs/comparison): threads next to the OpenAI Agents SDK, Pydantic AI, Deep Agents, Strands and the Claude Agent SDK
- [**API reference**](https://threadsai.dev/docs/reference/overview): every public function and type, in both languages

## Repository layout

| Folder | What's in it |
|---|---|
| `spec/` | The shared contract both languages follow: event log schema, public API map, SQLite layout, tool catalog and the conformance cases both implementations must pass |
| `typescript/` | The TypeScript framework (`@threads/*` packages) |
| `python/` | The Python framework (`threads`, with optional extras per provider) |
| `docs/` | The documentation site |
| `scripts/` | `generate.sh` (rebuild generated code) and repo checks |
| `AGENTS.md` | Coding rules for contributors and coding agents |

## Contributing

Read [`AGENTS.md`](AGENTS.md) first. A feature starts in `spec/`, lands in both languages, and passes the same conformance cases in each.

## License

[Apache-2.0](LICENSE)
