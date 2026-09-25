import type { RunResult, Store } from "../../src";
import { openStore } from "../../src/agent/sqlite";
import type { EventOf } from "../../src/fold/state";
import type { KnownEvent, Principal } from "../../src/log";
import { knownEvents } from "../../src/reduce";
import { verifyRequests } from "../../src/render";
import type { LogStore } from "../../src/store";
import { unwrap } from "../store/helpers";

// Shared by the memory and knowledge behavior tests: scripted turns and log readers.

const usage = { input_tokens: 10, output_tokens: 2 };
type Turn = Readonly<Record<string, unknown>>;

export const say = (text: string): Turn => ({
  content: [{ type: "text", text }],
  stop_reason: "end_turn",
  usage,
});
export const use = (
  name: string,
  input: Record<string, unknown>,
  id: string,
): Turn => ({
  content: [{ type: "tool_use", call_id: id, name, input }],
  stop_reason: "tool_use",
  usage,
});

export const ALICE: Principal = {
  issuer: "api",
  tenant: "acme",
  subject: "alice",
};
export const MALLORY: Principal = {
  issuer: "api",
  tenant: "globex",
  subject: "alice",
};

/** The branch's events, with every recorded request re-verified (C7 and request_ref). */
export async function eventsOf<T>(
  result: RunResult<T>,
): Promise<readonly KnownEvent[]> {
  const { log, artifacts } = await openStore(result.thread.store);
  const events = knownEvents(unwrap(await log.read(result.thread.branch)));
  unwrap(await verifyRequests(events, artifacts));
  return events;
}

/** The bytes of every turn request the run recorded, in order. */
export async function requestsOf<T>(
  result: RunResult<T>,
): Promise<readonly string[]> {
  const { artifacts } = await openStore(result.thread.store);
  const decoder = new TextDecoder();
  const requests = (await eventsOf(result)).flatMap((e) =>
    e.type === "model_request" ? [e.data.request_ref.sha256] : [],
  );
  const out: string[] = [];
  for (const sha256 of requests)
    out.push(decoder.decode(unwrap(await artifacts.get(sha256))));
  return out;
}

export function injectedOf(
  events: readonly KnownEvent[],
): readonly EventOf<"injected">["data"][] {
  return events.flatMap((e) => (e.type === "injected" ? [e.data] : []));
}

export function resultsOf(
  events: readonly KnownEvent[],
): readonly EventOf<"tool_result">["data"][] {
  return events.flatMap((e) => (e.type === "tool_result" ? [e.data] : []));
}

export function typesOf(events: readonly KnownEvent[]): readonly string[] {
  return events.map((e) => e.type);
}

export async function driverOf(store: Store): Promise<LogStore> {
  return (await openStore(store)).log;
}
