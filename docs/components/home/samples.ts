// Landing page code samples. Like every docs example, each one is type-checked with tsc (TS) and pyright
// (Python) against the repo before it changes (docs/README.md, "Writing guides").

export type Sample = { ts: string; py: string };

// The Quickstart agent, unchanged.
export const QUICKSTART: Sample = {
  ts: `import { agent, sqlite, tool } from "@threads/core";
import { anthropic } from "@threads/anthropic";
import { z } from "zod";

const getWeather = tool({
  name: "get_weather",
  description: "Get the weather for a city.",
  input: z.object({ city: z.string() }),
  effect: "read_only",
  execute: async ({ city }) => \`It is sunny in \${city}.\`,
});

const weather = agent({
  name: "weather",
  instructions: "Answer questions about the weather.",
  model: anthropic("claude-sonnet-5"),
  tools: [getWeather],
});

const result = await weather.run("What is the weather in Paris?", {
  store: sqlite(".threads"),
});
if (result.status === "completed") console.log(result.output);`,
  py: `import asyncio

from pydantic import BaseModel

from threads import RunContext, agent, sqlite, tool
from threads.anthropic import anthropic


class WeatherInput(BaseModel):
    city: str


async def get_weather(args: WeatherInput, ctx: RunContext[None]) -> str:
    return f"It is sunny in {args.city}."


weather_tool = tool(
    name="get_weather",
    description="Get the weather for a city.",
    input=WeatherInput,
    effect="read_only",
    execute=get_weather,
)

weather = agent(
    name="weather",
    instructions="Answer questions about the weather.",
    model=anthropic("claude-sonnet-5"),
    tools=[weather_tool],
)


async def main() -> None:
    result = await weather.run("What is the weather in Paris?", store=sqlite(".threads"))
    if result.status == "completed":
        print(result.output)


asyncio.run(main())`,
};

// Turning on OpenTelemetry export, from the Observability guide.
export const TELEMETRY: Sample = {
  ts: `import { host } from "@threads/host";
import { otel } from "@threads/otel";

// OTEL_EXPORTER_OTLP_ENDPOINT=https://api.honeycomb.io
// OTEL_EXPORTER_OTLP_HEADERS=x-honeycomb-team=<your key>
// OTEL_SERVICE_NAME=support-bot
export default host({
  store,
  agents: { support },
  telemetry: otel(),
});`,
  py: `from threads.host import host
from threads.otel import otel

# OTEL_EXPORTER_OTLP_ENDPOINT=https://api.honeycomb.io
# OTEL_EXPORTER_OTLP_HEADERS=x-honeycomb-team=<your key>
# OTEL_SERVICE_NAME=support-bot
app = host(
    store=store,
    agents={"support": support},
    telemetry=otel(),
)`,
};

// Saving a real turn and checking it, from the Running evals guide.
export const EVALS: Sample = {
  ts: `// Once, from a real thread you liked.
await thread.saveCase("refund-policy", {
  externalEffects: "stub",
});

// In CI: no model calls, no API keys, no network.
const report = await runEvals({
  cases: "cases",
  agents: [support],
});
console.log(report.summary); // "12 passed, 0 failed"`,
  py: `# Once, from a real thread you liked.
await thread.save_case(
    "refund-policy", external_effects="stub"
)

# In CI: no model calls, no API keys, no network.
report = await run_evals(
    cases="cases", agents=(support,)
)
print(report.summary)  # "12 passed, 0 failed"`,
};

export type UseCase = Sample & { id: string; title: string; body: string; href: string };

export const USE_CASES: UseCase[] = [
  {
    id: "quickstart",
    title: "Your first agent",
    body: "One tool, one agent, one run. This is the whole quickstart program, and the thread it writes is the one timeline() reads back.",
    href: "/docs/quickstart",
    ...QUICKSTART,
  },
  {
    id: "slack",
    title: "Support bot on Slack",
    body: "Each Slack conversation becomes a thread. Risky actions get Approve and Deny buttons in the channel.",
    href: "/docs/host/slack",
    ts: `import { agent, secret, sqlite } from "@threads/core";
import { anthropic } from "@threads/anthropic";
import { host } from "@threads/host";
import { slack } from "@threads/slack";

const support = agent({
  name: "support",
  instructions: "Answer questions about our product. Be brief.",
  model: anthropic("claude-sonnet-5"),
});

export default host({
  store: sqlite(".threads"),
  agents: { support },
  channels: {
    slack: slack({
      agent: "support",
      signingSecret: secret("SLACK_SIGNING_SECRET"),
      botToken: secret("SLACK_BOT_TOKEN"),
    }),
  },
});`,
    py: `from threads import agent, secret, sqlite
from threads.anthropic import anthropic
from threads.host import host
from threads.slack import slack

support = agent(
    name="support",
    instructions="Answer questions about our product. Be brief.",
    model=anthropic("claude-sonnet-5"),
)

app = host(
    store=sqlite(".threads"),
    agents={"support": support},
    channels={
        "slack": slack(
            agent="support",
            signing_secret=secret("SLACK_SIGNING_SECRET"),
            bot_token=secret("SLACK_BOT_TOKEN"),
        ),
    },
)`,
  },
  {
    id: "coding",
    title: "Coding agent in a sandbox",
    body: "The agent gets a shell and files in its own machine, with no internet and none of your keys.",
    href: "/docs/sandboxes/overview",
    ts: `import { agent, sqlite } from "@threads/core";
import { anthropic } from "@threads/anthropic";
import { e2b } from "@threads/e2b";

const coder = agent({
  name: "coder",
  instructions: "Fix the failing tests in /workspace.",
  model: anthropic("claude-sonnet-5"),
  sandbox: e2b(), // bash, read, write, edit, grep...
});

const result = await coder.run("Make the test suite pass.", {
  store: sqlite(".threads"),
});
if (result.status === "completed") console.log(result.output);`,
    py: `from threads import agent, sqlite
from threads.anthropic import anthropic
from threads.e2b import e2b

coder = agent(
    name="coder",
    instructions="Fix the failing tests in /workspace.",
    model=anthropic("claude-sonnet-5"),
    sandbox=e2b(),  # bash, read, write, edit, grep...
)

result = coder.run_sync("Make the test suite pass.", store=sqlite(".threads"))
if result.status == "completed":
    print(result.output)`,
  },
  {
    id: "research",
    title: "Research team",
    body: "A lead hands questions to a researcher on a cheaper model, then writes the report from what comes back.",
    href: "/docs/multi-agent/subagents",
    ts: `import { agent, exa, secret, sqlite } from "@threads/core";
import { anthropic } from "@threads/anthropic";
import { openai } from "@threads/openai";

const search = exa(secret("EXA_API_KEY"));

const researcher = agent({
  name: "researcher",
  instructions: "Answer one question from the web. Cite your sources.",
  model: openai("gpt-5.5"),
  web: { fetch: true, search },
});

const lead = agent({
  name: "lead",
  instructions: "Split the topic into questions, ask the researcher each one, then write the report.",
  model: anthropic("claude-sonnet-5"),
  web: { fetch: true, search },
  subagents: [researcher],
});

const report = await lead.run("How do support teams use agents today?", {
  store: sqlite(".threads"),
});`,
    py: `from threads import agent, secret, sqlite
from threads.anthropic import anthropic
from threads.openai import openai
from threads.search import exa

search = exa(secret("EXA_API_KEY"))

researcher = agent(
    name="researcher",
    instructions="Answer one question from the web. Cite your sources.",
    model=openai("gpt-5.5"),
    web={"fetch": True, "search": search},
)

lead = agent(
    name="lead",
    instructions="Split the topic into questions, ask the researcher each one, then write the report.",
    model=anthropic("claude-sonnet-5"),
    web={"fetch": True, "search": search},
    subagents=[researcher],
)

report = lead.run_sync("How do support teams use agents today?", store=sqlite(".threads"))`,
  },
  {
    id: "scheduled",
    title: "Scheduled job",
    body: "Run an agent on a cron schedule in any time zone. Each occurrence runs once, even across restarts.",
    href: "/docs/host/schedules",
    ts: `import { agent, sqlite } from "@threads/core";
import { anthropic } from "@threads/anthropic";
import { host } from "@threads/host";

const digest = agent({
  name: "digest",
  instructions: "Write a short digest of what changed since the last run.",
  model: anthropic("claude-sonnet-5"),
});

export default host({
  store: sqlite(".threads"),
  agents: { digest },
  schedules: [
    {
      id: "weekday_digest",
      agent: "digest",
      cron: "0 9 * * 1-5",
      timezone: "Europe/Berlin",
      input: "Write today's digest.",
    },
  ],
});`,
    py: `from threads import agent, sqlite
from threads.anthropic import anthropic
from threads.host import Schedule, host

digest = agent(
    name="digest",
    instructions="Write a short digest of what changed since the last run.",
    model=anthropic("claude-sonnet-5"),
)

app = host(
    store=sqlite(".threads"),
    agents={"digest": digest},
    schedules=[
        Schedule(
            id="weekday_digest",
            agent="digest",
            cron="0 9 * * 1-5",
            timezone="Europe/Berlin",
            input="Write today's digest.",
        ),
    ],
)`,
  },
];
