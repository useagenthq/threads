import { expect, test } from "bun:test";
import { agent, scriptedModel, sqlite } from "../../src";

// The Teams guide's second example (docs/content/docs/(guides)/multi-agent/teams.mdx, "Waiting for
// members"), as written there, so the page's program stays runnable.

test("the Teams guide wait example runs and prints the lead's summary", async () => {
  const printed: string[] = [];
  const log = (line: string): void => {
    printed.push(line);
  };

  const usage = { input_tokens: 10, output_tokens: 2 };
  const say = (text: string) => ({
    content: [{ type: "text", text }],
    stop_reason: "end_turn",
    usage,
  });
  const call = (id: string, name: string, input: unknown) => ({
    content: [{ type: "tool_use", call_id: id, name, input }],
    stop_reason: "tool_use",
    usage,
  });

  const researcher = agent({
    name: "researcher",
    instructions: "Research the topic you are given. Answer in one sentence.",
    model: scriptedModel({
      responses: [
        say("Battery pack prices fell this year."),
        say("Solar module prices fell too."),
      ],
    }),
  });

  const lead = agent({
    name: "lead",
    instructions:
      "Start a researcher per topic, wait for both, then summarize.",
    model: scriptedModel({
      responses: [
        call("c1", "start", { agent: "researcher", task: "Battery prices." }),
        call("c2", "start", { agent: "researcher", task: "Solar prices." }),
        call("c3", "wait", { members: ["researcher-1", "researcher-2"] }),
        say("Battery and solar prices both fell this year."),
        say("Their reports are in: battery and solar prices both fell."),
      ],
    }),
    team: [researcher],
  });

  const r = await lead.run("Compare battery and solar prices.", {
    store: sqlite(":memory:"),
  });
  if (r.status === "completed") log(r.output);

  expect(printed).toEqual([
    "Their reports are in: battery and solar prices both fell.",
  ]);
});
