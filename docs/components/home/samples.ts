// Landing page code samples. Like every docs example, each one is type-checked with tsc (TS) and pyright
// (Python) against the repo before it changes (docs/README.md, "Writing guides").

export type Sample = { ts: string; py: string };

// The first sample a visitor meets, so it shows one idea: a name, instructions and a model, run once.
// Tools, approvals and the rest are the Learn tutorials' job.
export const FIRST_AGENT: Sample = {
  ts: `import { agent } from "@threads/core";
import { anthropic } from "@threads/anthropic";

const support = agent({
  name: "support",
  instructions: "Answer questions about our product. Be brief.",
  model: anthropic("claude-sonnet-5"),
});

const result = await support.run("Can I return a keyboard?");
if (result.status === "completed") console.log(result.output);`,
  py: `from threads import agent
from threads.anthropic import anthropic

support = agent(
    name="support",
    instructions="Answer questions about our product. Be brief.",
    model=anthropic("claude-sonnet-5"),
)

result = support.run_sync("Can I return a keyboard?")
if result.status == "completed":
    print(result.output)`,
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

// Saving a real turn and checking it, from the Running evals guide. What report.summary prints is the
// last line of the terminal beside it, so the sample does not quote a count of its own.
export const EVALS: Sample = {
  ts: `// Once, from a real thread you liked.
await thread.saveCase("refund-policy", {
  expect: { must: [{ type: "turn_completed" }] },
  externalEffects: "stub",
});

// In CI: no model calls, no API keys, no network.
const report = await runEvals({
  cases: "cases",
  agents: [support],
});
console.log(report.summary);`,
  py: `# Once, from a real thread you liked.
await thread.save_case(
    "refund-policy",
    expect=CaseExpectation(must=({"type": "turn_completed"},)),
    external_effects="stub",
)

# In CI: no model calls, no API keys, no network.
report = await run_evals(cases="cases", agents=(support,))
print(report.summary)`,
};

export type UseCase = Sample & { id: string; title: string; body: string; href: string };

export const USE_CASES: UseCase[] = [
  {
    id: "first-agent",
    title: "Your first agent",
    body: "A name, instructions and a model, and that is the program. The run writes its log to .threads, which is the thread timeline() reads back.",
    href: "/docs/learn/tutorials/first-agent",
    ...FIRST_AGENT,
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
    ts: `import { agent } from "@threads/core";
import { anthropic } from "@threads/anthropic";
import { e2b } from "@threads/e2b";

const coder = agent({
  name: "coder",
  instructions: "Fix the failing tests in /workspace.",
  model: anthropic("claude-sonnet-5"),
  sandbox: e2b(), // bash, read, write, edit, grep...
  permissions: { mode: "accept_edits", allow: ["bash(*)"] },
});

const result = await coder.run("Make the test suite pass.");
if (result.status === "completed") console.log(result.output);`,
    py: `from threads import agent
from threads.anthropic import anthropic
from threads.e2b import e2b

coder = agent(
    name="coder",
    instructions="Fix the failing tests in /workspace.",
    model=anthropic("claude-sonnet-5"),
    sandbox=e2b(),  # bash, read, write, edit, grep...
    permissions={"mode": "accept_edits", "allow": ["bash(*)"]},
)

result = coder.run_sync("Make the test suite pass.")
if result.status == "completed":
    print(result.output)`,
  },
  {
    id: "research",
    title: "Research team",
    body: "A lead hands questions to a researcher on a cheaper model, then writes the report from what comes back.",
    href: "/docs/multi-agent/subagents",
    ts: `import { agent, exa, secret } from "@threads/core";
import { anthropic } from "@threads/anthropic";
import { openai } from "@threads/openai";

const researcher = agent({
  name: "researcher",
  instructions: "Answer one question from the web. Cite your sources.",
  model: openai("gpt-5.5"),
  web: { fetch: true, search: exa(secret("EXA_API_KEY")) },
});

const lead = agent({
  name: "lead",
  instructions: "Ask the researcher one question per angle, then write the report.",
  model: anthropic("claude-sonnet-5"),
  subagents: [researcher],
});

const report = await lead.run("How do support teams use agents today?");
if (report.status === "completed") console.log(report.output);`,
    py: `from threads import agent, secret
from threads.anthropic import anthropic
from threads.openai import openai
from threads.search import exa

researcher = agent(
    name="researcher",
    instructions="Answer one question from the web. Cite your sources.",
    model=openai("gpt-5.5"),
    web={"fetch": True, "search": exa(secret("EXA_API_KEY"))},
)

lead = agent(
    name="lead",
    instructions="Ask the researcher one question per angle, then write the report.",
    model=anthropic("claude-sonnet-5"),
    subagents=[researcher],
)

report = lead.run_sync("How do support teams use agents today?")
if report.status == "completed":
    print(report.output)`,
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
