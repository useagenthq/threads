import { expect, test } from "bun:test";
import { agent, scriptedModel, sqlite, type TeamCursor } from "../../src";

// The Teams guide's complete example (docs/content/docs/(guides)/multi-agent/teams.mdx), as
// written there, so the page's program stays runnable.

test("the Teams guide example runs and prints the lead's last answer", async () => {
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
  const start = (agentName: string, task: string) => ({
    content: [
      {
        type: "tool_use",
        call_id: "c1",
        name: "start",
        input: { agent: agentName, task },
      },
    ],
    stop_reason: "tool_use",
    usage,
  });

  const researcher = agent({
    name: "researcher",
    instructions: "Research the topic you are given. Answer in one sentence.",
    model: scriptedModel({
      responses: [say("Battery pack prices fell this year.")],
    }),
  });

  const lead = agent({
    name: "lead",
    instructions: "Start a researcher on the topic, then report what it found.",
    model: scriptedModel({
      responses: [
        start("researcher", "Battery prices this year."),
        say("I started a researcher."),
        say("The researcher reports that battery pack prices fell this year."),
      ],
    }),
    team: [researcher],
  });

  const r = await lead.run("Report on battery prices.", {
    store: sqlite(":memory:"),
  });
  if (r.status === "completed") log(r.output);
  log(`team ${r.team.ref.id}`);

  expect(printed[0]).toBe(
    "The researcher reports that battery pack prices fell this year.",
  );
  expect(printed[1]).toMatch(/^team [0-9a-f-]{36}$/);
});

test("the guide's follow snippet tails the feed, then resumes from its last cursor", async () => {
  const usage = { input_tokens: 10, output_tokens: 2 };
  const say = (text: string) => ({
    content: [{ type: "text", text }],
    stop_reason: "end_turn",
    usage,
  });
  const researcher = agent({
    name: "researcher",
    instructions: "Research the topic you are given. Answer in one sentence.",
    model: scriptedModel({
      responses: [say("Battery pack prices fell this year.")],
    }),
  });
  const lead = agent({
    name: "lead",
    instructions: "Start a researcher on the topic, then report what it found.",
    model: scriptedModel({
      responses: [
        {
          content: [
            {
              type: "tool_use",
              call_id: "c1",
              name: "start",
              input: { agent: "researcher", task: "Battery prices this year." },
            },
          ],
          stop_reason: "tool_use",
          usage,
        },
        say("I started a researcher."),
        say("The researcher reports that battery pack prices fell this year."),
      ],
    }),
    team: [researcher],
  });
  const r = await lead.run("Report on battery prices.", {
    store: sqlite(":memory:"),
  });
  const team = r.team;
  const printed: string[] = [];

  // A live tail. It ends when the team closes, or when you stop iterating.
  let last: TeamCursor | undefined;
  for await (const item of team.events({ follow: true })) {
    last = item.cursor;
    // The feed was rebuilt under a new epoch: read on from the cursor it names.
    if (item.kind === "epoch_restarted") continue;
    const who =
      item.source.kind === "member"
        ? item.source.member.name
        : item.source.kind;
    printed.push(
      `${item.cursor.epoch}:${item.cursor.offset} ${who} ${item.event.type}`,
    );
    if (item.event.type === "member_started") break;
  }

  // Later, or in another process: exactly where that reader stopped.
  if (last === undefined) throw new Error("the feed was empty");
  const rest: string[] = [];
  for await (const item of team.events({ after: last }))
    rest.push(item.kind === "event" ? item.event.type : item.kind);

  expect(printed[0]).toBe("1:1 team team_opened");
  expect(printed.at(-1)).toMatch(/ member_started$/);
  expect(rest.length).toBeGreaterThan(0);
});
