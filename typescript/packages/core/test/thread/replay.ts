import { readdirSync, readFileSync } from "node:fs";
import { join } from "node:path";
import { z } from "zod";
import { openThread } from "../../src";
import { storeOf } from "../../src/agent/sqlite";
import { BranchId, KnownEvent, ThreadId } from "../../src/log";
import { knownEvents } from "../../src/reduce";
import type { Thread } from "../../src/thread/handle";
import { type Fixture, fixture, unwrap } from "../store/helpers";

// A corpus case's log re-appended through this implementation's writer, so a test can go on
// from it: the case may be another implementation's branch, so the event ids its events
// reference are mapped to the new ones.

const CASES = join(import.meta.dir, "../../../../../spec/conformance/cases");
const Header = z.object({ thread_id: ThreadId, branch_id: BranchId });

/** Puts the case's artifacts in the fixture's store. */
export async function caseArtifacts(name: string, f: Fixture): Promise<void> {
  const dir = join(CASES, name, "artifacts");
  for (const art of readdirSync(dir))
    await f.artifacts.put(readFileSync(join(dir, art)));
}

export type Replayed = {
  readonly f: Fixture;
  readonly thread: Thread;
  readonly threadId: ThreadId;
  readonly branch: BranchId;
  readonly events: () => Promise<readonly KnownEvent[]>;
};

/** The first `events` events of the case's `file`, in a fresh store of tenant acme. */
export async function replayCase(
  name: string,
  events: number,
  file = "log.jsonl",
  given?: Fixture,
): Promise<Replayed> {
  const f = given ?? (await fixture("acme"));
  const dir = join(CASES, name);
  await caseArtifacts(name, f);
  const lines = readFileSync(join(dir, file), "utf8")
    .split("\n")
    .slice(0, events + 1);
  const header = Header.parse(JSON.parse(lines[0] ?? ""));
  unwrap(await f.store.createBranch(header.thread_id, header.branch_id));
  const writer = unwrap(await f.store.acquire(header.branch_id, "setup"));
  const ids = new Map<string, string>();
  for (let line of lines.slice(1)) {
    for (const [from, to] of ids) line = line.replaceAll(from, to);
    const {
      seq: _seq,
      event_id: old,
      thread_id: _thread,
      branch_id: _branch,
      epoch: _epoch,
      time: _time,
      prev_hash: _prev,
      ...draft
    } = KnownEvent.parse(JSON.parse(line));
    const [added] = unwrap(await writer.append([draft]));
    if (added?.kind === "event") ids.set(old, added.event.event_id);
  }
  await writer.release();
  const store = storeOf({ log: f.store, artifacts: f.artifacts });
  const thread = unwrap(await openThread(store, header.thread_id));
  const read = async () =>
    knownEvents(unwrap(await f.store.read(header.branch_id)));
  return {
    f,
    thread,
    threadId: header.thread_id,
    branch: header.branch_id,
    events: read,
  };
}
