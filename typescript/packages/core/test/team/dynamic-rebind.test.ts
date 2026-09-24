import { describe, expect, test } from "bun:test";
import { z } from "zod";
import {
  agent,
  dynamicAgent,
  type McpServer,
  scriptedModel,
  sqlite,
  tool,
} from "../../src";
import { startedOf, startSpecialist } from "./dynamic-kit";
import { assertTeamReplays } from "./kit";
import { events, logOf, memberEvents, receipts, say, types } from "./run-kit";

// Rebind of a dynamic member (spec/schema/README.md, Teams, "Dynamic members"): materialize
// rebuilds the pin from the template by name with the recorded define, never re-resolving it. A
// chosen tool that is gone is pin_unavailable; a tool that now pins differently is pin_mismatch.
// Either is one recorded end-of-member append, with no model request then or on a later run.

/** An MCP server whose nth connect lists `connects[n]` ({name, description}). */
function drifting(
  connects: readonly { readonly name: string; readonly description: string }[],
): McpServer {
  let n = 0;
  return {
    kind: "mcp",
    name: "docs",
    connect: async () => {
      const listed = connects[Math.min(n, connects.length - 1)];
      n += 1;
      if (listed === undefined) throw new Error("no connects");
      const close = async (): Promise<void> => undefined;
      return {
        tools: [
          tool({
            name: `mcp__docs__${listed.name}`,
            description: listed.description,
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

const A = { name: "read_a", description: "Read a document." };

async function failedRebind(
  later: { readonly name: string; readonly description: string },
  code: "pin_unavailable" | "pin_mismatch",
): Promise<void> {
  const store = sqlite(":memory:");
  const member = scriptedModel({ responses: [say("never")] });
  // Two connects at start (the template's tools, then the member's pin), then the rebind's.
  const template = dynamicAgent({
    name: "specialist",
    models: { fast: member },
    tools: [drifting([A, A, later])],
  });
  const lead = agent({
    name: "lead",
    model: scriptedModel({
      responses: [
        startSpecialist("c1", { tools: ["mcp__docs__read_a"] }),
        say("Started."),
        say("The specialist could not start."),
        say("Still here."),
      ],
    }),
    team: [template],
  });
  const r = await lead.run("Work.", { store });
  expect(r.status === "completed" && r.output).toBe(
    "The specialist could not start.",
  );
  const [started] = startedOf(await events(store, r.thread));
  expect(started?.data.define?.tools).toEqual(["mcp__docs__read_a"]);
  const log = await memberEvents(store, r.team.ref.id, "specialist-1");
  expect(types(log)).toEqual([
    "thread_started",
    "user_input",
    "turn_completed",
    "member_ended",
    "message_sent",
  ]);
  const ended = log.find((e) => e.type === "member_ended");
  expect(ended?.type === "member_ended" && ended.data.result).toMatchObject({
    status: "failed",
    error: { code },
  });
  expect(receipts(await events(store, r.thread), "member_ended")).toHaveLength(
    1,
  );
  // A later run (a restart) records nothing more for it, and makes no model request.
  const again = await lead.run("Anything else?", { store, thread: r.thread });
  expect(again.status === "completed" && again.output).toBe("Still here.");
  expect(await memberEvents(store, r.team.ref.id, "specialist-1")).toHaveLength(
    log.length,
  );
  expect(member.remaining()).toBe(1);
  assertTeamReplays(await logOf(store), r.team.ref.id);
}

describe("rebinding a dynamic member", () => {
  test("a chosen tool the template no longer has: pin_unavailable, recorded once", async () => {
    await failedRebind(
      { name: "read_b", description: A.description },
      "pin_unavailable",
    );
  });

  test("a chosen tool whose description changed: pin_mismatch", async () => {
    await failedRebind(
      { name: "read_a", description: "Read any file." },
      "pin_mismatch",
    );
  });
});
