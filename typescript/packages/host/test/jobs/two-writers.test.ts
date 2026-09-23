import { afterEach, describe, expect, test } from "bun:test";
import {
  events,
  expireLeases,
  finish,
  go,
  kill,
  logged,
  oneWriterAtATime,
  reap,
  release,
  scratch,
  sends,
  spawn,
  waitAt,
} from "./drill";
import { rows } from "./worker";

// Job two-writers-fenced: two host processes on one store race for one
// branch. Only the lease holder dispatches; a holder whose lease ran out while it stalled is
// refused at the transport, appends nothing, and nothing is sent twice.

afterEach(reap);

/** A conversation whose message is durable in the inbox and not yet consumed by anyone. */
async function seeded(): Promise<string> {
  const dir = scratch();
  const seed = spawn("serve", dir, {
    DRILL_WEBHOOK: "1",
    DRILL_STOP_AT: "webhook_ack",
  });
  await waitAt(seed, "webhook_ack");
  await kill(seed);
  expireLeases(dir);
  return dir;
}

describe("two-writers-fenced", () => {
  test("two hosts consume one message: one run, one send", async () => {
    const dir = await seeded();
    const a = spawn("serve", dir, { DRILL_GO: "1" });
    const b = spawn("serve", dir, { DRILL_GO: "1" });
    go(dir);
    expect(await finish(a)).toBe(0);
    expect(await finish(b)).toBe(0);
    const log = events(dir);
    expect(log.filter((e) => e.type === "user_input")).toHaveLength(1);
    expect(sends(dir)).toHaveLength(1);
    expect(rows(dir, "model.jsonl")).toHaveLength(1);
    // The send went through the effect path under a lease: one effect_begin, durable before it.
    // Either host may issue the reply: any ready host issues a thread's missing replies
    // (spec/schema/README.md, "Channel replies"), and the run's lease is released before it.
    expect(log.filter((e) => e.type === "effect_begin")).toHaveLength(1);
    oneWriterAtATime(log);
    expect(logged(dir)).toBe("");
  }, 60_000);

  test("a stalled holder whose lease ran out is fenced at the transport", async () => {
    const dir = await seeded();
    const stale = spawn("serve", dir, { DRILL_STOP_AT: "effect_begin" });
    await waitAt(stale, "effect_begin");
    // Its reply's effect_begin is durable and its send is about to leave; its lease runs out.
    expireLeases(dir);
    const next = spawn("serve", dir);
    expect(await finish(next)).toBe(0);
    const taken = events(dir).map((e) => e.event_id);

    release(dir);
    expect(await finish(stale)).toBe(0);
    const log = events(dir);
    expect(log.map((e) => e.event_id)).toEqual(taken);
    const [sent] = rows(dir, "sends.jsonl");
    expect(sends(dir)).toHaveLength(1);
    expect(sent?.["pid"]).toBe(next.proc.pid);
    expect(rows(dir, "refused.jsonl")).toEqual([
      { key: sent?.["key"], pid: stale.proc.pid },
    ]);
    oneWriterAtATime(log);
    // The new holder recovered the in-doubt send under a newer epoch.
    const epochs = new Set(log.map((e) => e.epoch));
    expect(epochs.size).toBeGreaterThan(1);
  }, 60_000);
});
