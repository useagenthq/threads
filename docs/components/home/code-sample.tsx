import {
  CodeBlockTab,
  CodeBlockTabs,
  CodeBlockTabsList,
  CodeBlockTabsTrigger,
} from "fumadocs-ui/components/codeblock";
import { ServerCodeBlock } from "fumadocs-ui/components/codeblock.rsc";

// The Quickstart agent, unchanged: type-checked (TS) and pyright-checked (Python) against the repo.
const TS = `import { agent, sqlite, tool } from "@threads/core";
import { anthropic } from "@threads/anthropic";
import { z } from "zod";

const getWeather = tool({
  name: "get_weather",
  description: "Get the weather for a city.",
  input: z.object({ city: z.string() }),
  runs: "host",
  effect: "read_only",
  execute: async ({ city }) => \`It is sunny in \${city}.\`,
});

const weather = agent({
  name: "weather",
  instructions: "Answer questions about the weather.",
  model: anthropic({
    model: "claude-sonnet-5",
    maxTokens: 8192,
    contextWindow: 200_000,
    maxOutputTokens: 8192,
  }),
  tools: [getWeather],
});

const result = await weather.run("What is the weather in Paris?", {
  store: sqlite(".threads"),
});
if (result.status === "completed") console.log(result.output);`;

const PY = `import asyncio

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
    runs="host",
    effect="read_only",
    execute=get_weather,
)

weather = agent(
    name="weather",
    instructions="Answer questions about the weather.",
    model=anthropic("claude-sonnet-5", context_window=200_000, max_output_tokens=8192),
    tools=[weather_tool],
)


async def main() -> None:
    result = await weather.run("What is the weather in Paris?", store=sqlite(".threads"), deps=None)
    if result.status == "completed":
        print(result.output)


asyncio.run(main())`;

const LANGS = [
  { value: "TypeScript", lang: "ts", code: TS },
  { value: "Python", lang: "python", code: PY },
] as const;

/** TS/Python sample. Shares the "lang" tab group with the docs, so the choice carries over. */
export function CodeSample() {
  return (
    <CodeBlockTabs
      groupId="lang"
      persist
      defaultValue="TypeScript"
      className="my-0 h-full rounded-none border-0 bg-transparent"
    >
      <CodeBlockTabsList>
        {LANGS.map((l) => (
          <CodeBlockTabsTrigger key={l.value} value={l.value}>
            {l.value}
          </CodeBlockTabsTrigger>
        ))}
      </CodeBlockTabsList>
      {LANGS.map((l) => (
        <CodeBlockTab key={l.value} value={l.value}>
          <ServerCodeBlock
            code={l.code}
            lang={l.lang}
            codeblock={{ className: "my-0 rounded-none border-0" }}
          />
        </CodeBlockTab>
      ))}
    </CodeBlockTabs>
  );
}
