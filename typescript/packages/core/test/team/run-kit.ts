import type { sqlite, ThreadRef } from "../../src";
import { openStore } from "../../src/agent/sqlite";
import { BranchId, type KnownEvent, type TeamId } from "../../src/log";
import { knownEvents } from "../../src/reduce";
import type { LogStore } from "../../src/store";
import { memberRows } from "../../src/team/rows";
import { unwrap } from "../store/helpers";

// Shared by the team runtime tests: scripted answers and tool calls, and reads of a team's logs.

export const usage = { input_tokens: 10, output_tokens: 2 } as const;

export const say = (text: string): unknown => ({
  content: [{ type: "text", text }],
  stop_reason: "end_turn",
  usage,
});

export const call = (id: string, name: string, input: unknown): unknown => ({
  content: [{ type: "tool_use", call_id: id, name, input }],
  stop_reason: "tool_use",
  usage,
});

export const start = (id: string, agent: string, task: string): unknown =>
  call(id, "start", { agent, task });

type Store = ReturnType<typeof sqlite>;

export async function logOf(store: Store): Promise<LogStore> {
  return (await openStore(store)).log;
}

export async function events(
  store: Store,
  thread: ThreadRef,
): Promise<readonly KnownEvent[]> {
  const log = await logOf(store);
  return knownEvents(unwrap(log.read(thread.branch)));
}

/** A member's log, by its name in the team. */
export async function memberEvents(
  store: Store,
  team: TeamId,
  name: string,
): Promise<readonly KnownEvent[]> {
  const log = await logOf(store);
  const row = memberRows(log.driver, team).find((r) => r.name === name);
  if (row?.branch_id === undefined || row.branch_id === null)
    throw new Error(`member ${name} has no branch`);
  return knownEvents(unwrap(log.read(BranchId.parse(row.branch_id))));
}

export const types = (log: readonly KnownEvent[]): readonly string[] =>
  log.map((e) => e.type);

/** The received mail of one kind in a log. */
export const receipts = (
  log: readonly KnownEvent[],
  kind: string,
): readonly KnownEvent[] =>
  log.filter(
    (e) => e.type === "message_received" && e.data.envelope.kind === kind,
  );
