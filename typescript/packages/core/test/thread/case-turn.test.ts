import { describe, expect, test } from "bun:test";
import { readFileSync } from "node:fs";
import { join } from "node:path";
import { agent, scriptedModel } from "../../src";
import { openStore } from "../../src/agent/sqlite";
import type { KnownEvent } from "../../src/log";
import { knownEvents } from "../../src/reduce";
import { findTurn, offlineReason, type Turn } from "../../src/thread/case-turn";
import { verifyExport } from "../../src/verify";
import { CASES_DIR } from "../conformance/cases";
import { casesDir, memory, REFUND_TURN, say, support, use } from "../evals/kit";
import { unwrap } from "../store/helpers";

// Why a saved turn can't rerun offline (spec lane 22, A.4): each reason from what the turn holds.

async function refundTurn(): Promise<Turn> {
  const run = await support(REFUND_TURN).run("Refund order 42.", {
    store: memory(),
  });
  const { log } = await openStore(run.thread.store);
  return unwrap(findTurn(unwrap(log.read(run.thread.branch)), undefined));
}

const renamed = (e: KnownEvent, name: string): KnownEvent =>
  e.type === "tool_call" ? { ...e, data: { ...e.data, name } } : e;

describe("offlineReason", () => {
  test("a recorded refund turn reruns offline", async () => {
    const turn = await refundTurn();
    expect(offlineReason(turn, turn.events)).toBeUndefined();
  });

  test("a team tool call is team_calls; a spawn is child_threads", async () => {
    const turn = await refundTurn();
    const team = {
      ...turn,
      events: turn.events.map((e) => renamed(e, "send")),
    };
    const spawn = {
      ...turn,
      events: turn.events.map((e) => renamed(e, "spawn_agent")),
    };
    expect(offlineReason(team, team.events)).toEqual({ reason: "team_calls" });
    expect(offlineReason(spawn, spawn.events)).toEqual({
      reason: "child_threads",
    });
  });

  test("an effect begun and never settled is unsettled_effect", () => {
    const bytes = readFileSync(
      join(CASES_DIR, "cancelled-with-unsettled-effect", "log.jsonl"),
      "utf8",
    );
    const lines = bytes.split("\n");
    const upTo = lines.findIndex((l) => l.includes('"cancel_requested"'));
    const prefix = new TextEncoder().encode(
      `${lines.slice(0, upTo).join("\n")}\n`,
    );
    const events = knownEvents(unwrap(verifyExport(prefix)));
    const input = events.find((e) => e.type === "user_input");
    if (input?.type !== "user_input")
      throw new Error("the corpus log has an input");
    const turn: Turn = {
      restoreSeq: input.seq - 1,
      input,
      events: events.filter((e) => e.seq >= input.seq),
      unknown: [],
    };
    expect(offlineReason(turn, turn.events)).toEqual({
      reason: "unsettled_effect",
    });
  });

  test("a turn that spawned a subagent saves as child_threads", async () => {
    const reviewer = agent({
      name: "reviewer",
      model: scriptedModel({ responses: [say("fine")] }),
    });
    const lead = agent({
      name: "lead",
      model: scriptedModel({
        responses: [
          use("spawn_agent", { agent: "reviewer", prompt: "Review." }, "c1"),
          say("ok"),
        ],
      }),
      subagents: [reviewer],
    });
    const run = await lead.run("Review it.", { store: memory() });
    const saved = unwrap(
      await run.thread.saveCase("spawned", {
        expect: { must: [{ type: "agent_spawned" }] },
        externalEffects: "stub",
        dir: casesDir(),
      }),
    );
    expect(saved).toMatchObject({ portable: false, reason: "child_threads" });
  });
});
