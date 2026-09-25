import { afterEach, describe, expect, test } from "bun:test";
import { join } from "node:path";
import { LogStore, memoryArtifacts } from "@threads/core";
import { BranchId, type KnownEvent, knownEvents } from "@threads/core/host";
import { z } from "zod";
import { sqlAll } from "../sql";
import { TENANT } from "./api-worker";
import {
  expireLeases,
  finish,
  go,
  kill,
  logged,
  oneWriterAtATime,
  reap,
  scratch,
  spawn,
  waitAt,
} from "./drill";
import { drillDriver } from "./stores";
import { rows } from "./worker";

// Job api-run-kill-restart: a host running a run started through the run API is killed with
// SIGKILL, then a host that is given no input starts on the same store. The run's open turn goes
// on from the log; an effect that began is never run again; two hosts starting together resume
// it once.

afterEach(reap);

const API_WORKER = join(import.meta.dir, "api-worker.ts");

const api = (where: string, env: Record<string, string>) =>
  spawn("api", where, env, API_WORKER);

/** The drill run's branch, read on a connection closed right after. */
async function events(where: string): Promise<readonly KnownEvent[]> {
  const db = (await drillDriver(where)).db;
  try {
    const opened = await LogStore.open(db, Date.now, memoryArtifacts(), TENANT);
    if (!opened.ok) throw new Error(opened.error.message);
    const [row] = z
      .array(z.strictObject({ branch_id: BranchId }))
      .parse(await sqlAll(db, "SELECT branch_id FROM run_receipts", []));
    const read =
      row === undefined ? undefined : await opened.value.read(row.branch_id);
    return read?.ok === true ? knownEvents(read.value) : [];
  } finally {
    await db.close();
  }
}

const count = (all: readonly KnownEvent[], type: string): number =>
  all.filter((e) => e.type === type).length;

/** A started run whose host was killed at `point`, its lease still live. */
async function killed(
  point: string,
  env: Record<string, string> = {},
): Promise<string> {
  const dir = scratch();
  const first = api(dir, { ...env, DRILL_START: "1", DRILL_STOP_AT: point });
  await waitAt(first, point);
  await kill(first);
  return dir;
}

/** The same, once its lease has run out. */
async function crashed(
  point: string,
  env: Record<string, string> = {},
): Promise<string> {
  const dir = await killed(point, env);
  await expireLeases(dir);
  return dir;
}

describe("api-run-kill-restart", () => {
  test("killed at the model request: a host given no input completes the turn", async () => {
    const dir = await crashed("model_request");
    const acked = (await events(dir)).map((e) => e.event_id);

    expect(await finish(api(dir, {}))).toBe(0);
    const after = await events(dir);
    expect(after.map((e) => e.event_id).slice(0, acked.length)).toEqual(acked);
    expect(count(after, "user_input")).toBe(1);
    expect(count(after, "model_response")).toBe(1);
    expect(after.at(-1)?.type).toBe("turn_completed");
    oneWriterAtATime(after);

    // A second restart finds nothing to do.
    const settled = after.map((e) => e.event_id);
    expect(await finish(api(dir, {}))).toBe(0);
    expect((await events(dir)).map((e) => e.event_id)).toEqual(settled);
    expect(logged(dir)).toBe("");
  }, 60_000);

  test("restarted at once: the dead host's lease refuses it, then runs out, and the turn completes", async () => {
    const dir = await killed("model_request");
    const next = api(dir, {});
    // Its first passes find the branch leased by the dead host.
    await Bun.sleep(2_000);
    expect(count(await events(dir), "turn_completed")).toBe(0);
    await expireLeases(dir);
    expect(await finish(next)).toBe(0);
    const after = await events(dir);
    expect(count(after, "model_response")).toBe(1);
    expect(after.at(-1)?.type).toBe("turn_completed");
    oneWriterAtATime(after);
    expect(logged(dir)).toBe("");
  }, 60_000);

  test("killed inside an effect: the restart parks it and never runs it again", async () => {
    const dir = await crashed("effect_begin", { DRILL_CHARGE: "1" });
    expect(rows(dir, "charges.jsonl")).toHaveLength(1);

    expect(await finish(api(dir, { DRILL_CHARGE: "1" }))).toBe(0);
    const after = await events(dir);
    expect(rows(dir, "charges.jsonl")).toHaveLength(1);
    expect(count(after, "effect_begin")).toBe(1);
    expect(count(after, "parked")).toBe(1);
    expect(count(after, "turn_completed")).toBe(0);
    oneWriterAtATime(after);
    expect(logged(dir)).toBe("");
  }, 60_000);

  test("two hosts starting together resume it once", async () => {
    const dir = await crashed("model_request");
    const before = rows(dir, "model.jsonl").length;
    const a = api(dir, { DRILL_GO: "1" });
    const b = api(dir, { DRILL_GO: "1" });
    go(dir);
    expect(await finish(a)).toBe(0);
    expect(await finish(b)).toBe(0);
    const after = await events(dir);
    expect(rows(dir, "model.jsonl").length - before).toBe(1);
    expect(count(after, "model_response")).toBe(1);
    expect(count(after, "turn_completed")).toBe(1);
    oneWriterAtATime(after);
    expect(logged(dir)).toBe("");
  }, 60_000);
});
