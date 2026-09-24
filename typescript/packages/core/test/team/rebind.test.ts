import { describe, expect, test } from "bun:test";
import { z } from "zod";
import { agent, type McpServer, scriptedModel, sqlite, tool } from "../../src";
import { assertTeamReplays } from "./kit";
import {
  events,
  logOf,
  memberEvents,
  receipts,
  say,
  start,
  types,
} from "./run-kit";

// A failed rebind (spec/schema/README.md, "Teams", "A failed rebind"): materialize finds the
// member's definition changed (pin_mismatch) or not runnable here (pin_unavailable). One append
// opens the member's branch and ends it failed; no model request is ever made for it, not then and
// not on any later run; the task notification wakes the lead.

/** An MCP server whose nth connect lists `connects[n]`'s tool, or throws past the list. */
function drifting(connects: readonly string[]): McpServer {
  let n = 0;
  return {
    kind: "mcp",
    name: "docs",
    connect: async () => {
      const name = connects[n];
      n += 1;
      if (name === undefined) throw new Error("the docs server is gone");
      const close = async (): Promise<void> => undefined;
      return {
        tools: [
          tool({
            name: `mcp__docs__${name}`,
            description: "Read a document.",
            input: z.object({ id: z.string() }),
            runs: "host",
            effect: "read_only",
            execute: async () => "text",
          }),
        ],
        close,
        [Symbol.asyncDispose]: close,
      };
    },
  };
}

async function failedRebind(
  connects: readonly string[],
  code: "pin_mismatch" | "pin_unavailable",
): Promise<void> {
  const store = sqlite(":memory:");
  const model = scriptedModel({ responses: [say("never")] });
  const researcher = agent({
    name: "researcher",
    model,
    tools: [drifting(connects)],
  });
  const lead = agent({
    name: "lead",
    model: scriptedModel({
      responses: [
        start("c1", "researcher", "Read the docs."),
        say("Started."),
        say("The researcher could not start."),
        say("Still here."),
      ],
    }),
    team: [researcher],
  });
  const r = await lead.run("Work.", { store });
  expect(r.status === "completed" && r.output).toBe(
    "The researcher could not start.",
  );
  const member = await memberEvents(store, r.team.ref.id, "researcher-1");
  expect(types(member)).toEqual([
    "thread_started",
    "user_input",
    "turn_completed",
    "member_ended",
    "message_sent",
  ]);
  const ended = member.find((e) => e.type === "member_ended");
  expect(ended?.type === "member_ended" && ended.data.result).toMatchObject({
    status: "failed",
    error: { code, message: `rebind failed: ${code}` },
  });
  const lead0 = await events(store, r.thread);
  expect(receipts(lead0, "member_ended")).toHaveLength(1);
  // A later run of the lead (a restart) sends the member nothing and no model request.
  const again = await lead.run("Anything else?", { store, thread: r.thread });
  expect(again.status === "completed" && again.output).toBe("Still here.");
  const after = await memberEvents(store, r.team.ref.id, "researcher-1");
  expect(after).toHaveLength(member.length);
  expect(model.remaining()).toBe(1);
  assertTeamReplays(await logOf(store), r.team.ref.id);
}

describe("a failed rebind", () => {
  test("pin_mismatch: the definition changed between start and materialize", async () => {
    await failedRebind(["read_a", "read_b", "read_b"], "pin_mismatch");
  });

  test("pin_unavailable: the definition can't be set up here", async () => {
    await failedRebind(["read_a"], "pin_unavailable");
  });
});
