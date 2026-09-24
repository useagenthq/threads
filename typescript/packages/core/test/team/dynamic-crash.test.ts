import { expect, test } from "bun:test";
import { z } from "zod";
import {
  agent,
  type DynamicAgent,
  dynamicAgent,
  type McpServer,
  scriptedModel,
  tool,
} from "../../src";
import { hostRunner } from "../../src/agent/registry";
import { storeOf } from "../../src/agent/sqlite";
import { BranchId, TeamId, ThreadId } from "../../src/log";
import { knownEvents } from "../../src/reduce";
import { LogStore, memoryArtifacts, type SqliteDriver } from "../../src/store";
import { openBunSqlite } from "../../src/store/bun-sqlite";
import { memberRows } from "../../src/team/rows";
import { unwrap } from "../store/helpers";
import { startSpecialist } from "./dynamic-kit";
import { assertTeamReplays } from "./kit";
import { say } from "./run-kit";

// A crash drill for a dynamic member (spec/schema/README.md, Teams, "Dynamic members"): the
// process dies inside the member's materialize, after the lead's start recorded its define. The
// restart's template no longer has the chosen tool, an MCP tool (so the lead's own listing, and
// its pin, are unchanged): the member is rebound from the recorded choice, never re-resolved, so
// it ends pin_unavailable once, with no model request.

class Crash extends Error {}

/**
 * The same database, through a driver that dies at every materialize of the member: the process
 * never gets past it, whatever it retries.
 */
function crashing(base: SqliteDriver): SqliteDriver {
  return {
    ...base,
    run: (sql, params) => {
      if (sql.includes("UPDATE team_members SET branch_id"))
        throw new Crash("killed at the member's materialize");
      base.run(sql, params);
    },
  };
}

/** An MCP server that lists one tool, `name`. */
function docs(name: string): McpServer {
  return {
    kind: "mcp",
    name: "docs",
    connect: async () => {
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

const template = (tool: string, model = scriptedModel({ responses: [] })) =>
  dynamicAgent({
    name: "specialist",
    models: { fast: model },
    tools: [docs(tool)],
  });

function lead(template: DynamicAgent, fresh: boolean) {
  return agent({
    name: "lead",
    model: scriptedModel({
      responses: [
        ...(fresh
          ? [
              startSpecialist("c1", { tools: ["mcp__docs__read_a"] }),
              say("Started."),
            ]
          : [say("The specialist could not run.")]),
      ],
    }),
    team: [template],
  });
}

const Row = z.object({ thread_id: ThreadId, team_id: TeamId });

test("a crash at a dynamic member's materialize, then a template without its tool: pin_unavailable once", async () => {
  const db = openBunSqlite(":memory:");
  const artifacts = memoryArtifacts();
  const now = Date.now;
  const open = (driver: SqliteDriver) =>
    storeOf({
      log: unwrap(LogStore.open(driver, now, artifacts)),
      artifacts,
    });
  await expect(
    lead(template("read_a"), true).run("Is INV-1002 paid?", {
      store: open(crashing(db)),
    }),
  ).rejects.toThrow(Crash);

  // The restart's template lists read_b; the lead chose read_a.
  const member = scriptedModel({ responses: [say("never")] });
  const restarted = lead(template("read_b", member), false);
  const store = open(db);
  const log = unwrap(LogStore.open(db, now, artifacts));
  const [row] = z
    .array(Row)
    .parse(
      db.all("SELECT lead_thread_id AS thread_id, team_id FROM teams", []),
    );
  if (row === undefined)
    throw new Error("the lead's first append opened its team");
  const leadBranch = unwrap(log.mainBranch(row.thread_id));
  const runner = hostRunner(restarted);
  if (runner === undefined) throw new Error("agent() registers a host runner");
  const result = await runner.execute(
    {
      store,
      principal: { issuer: "api", tenant: "local", subject: "operator" },
      thread: { id: row.thread_id, branch: leadBranch, store },
    },
    [],
  );
  expect(result).toMatchObject({
    status: "completed",
    output: "The specialist could not run.",
  });

  const leadLog = knownEvents(unwrap(log.read(leadBranch)));
  const started = leadLog.filter((e) => e.type === "member_started");
  expect(started).toHaveLength(1);
  const [only] = memberRows(db, row.team_id).filter((r) => r.role === "member");
  if (only?.branch_id === null || only?.branch_id === undefined)
    throw new Error("the member's branch was opened by its failed rebind");
  const events = knownEvents(unwrap(log.read(BranchId.parse(only.branch_id))));
  expect(events.map((e) => e.type)).toEqual([
    "thread_started",
    "user_input",
    "turn_completed",
    "member_ended",
    "message_sent",
  ]);
  const ended = events.find((e) => e.type === "member_ended");
  expect(ended?.type === "member_ended" && ended.data.result).toMatchObject({
    status: "failed",
    error: { code: "pin_unavailable" },
  });
  expect(member.remaining()).toBe(1);
  assertTeamReplays(log, row.team_id);
});
