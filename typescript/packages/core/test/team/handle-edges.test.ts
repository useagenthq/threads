import { describe, expect, test } from "bun:test";
import { z } from "zod";
import { agent, StoreCorruptError, scriptedModel } from "../../src";
import { memberEntry } from "../../src/agent/registry";
import { storeOf } from "../../src/agent/sqlite";
import { teamHandle } from "../../src/agent/team/handle";
import { hydrated } from "../../src/agent/team/hydrate";
import { takeTeamLogMail } from "../../src/agent/team/log-mail";
import { type Json, StoredMemberResult } from "../../src/log";
import { knownEvents } from "../../src/reduce";
import { err, ok } from "../../src/result";
import {
  type ArtifactStore,
  LogStore,
  memoryArtifacts,
  type SqlValue,
  type StoreDriver,
  type Tx,
} from "../../src/store";
import { openBunSqlite } from "../../src/store/bun-sqlite";
import { materialize } from "../../src/team/materialize";
import { teamRow } from "../../src/team/rows";
import { logError } from "../../src/verify/error";
import { unwrap } from "../store/helpers";
import { specialist } from "./dynamic-kit";
import { assertTeamReplays, query, reading } from "./kit";
import { say, start } from "./run-kit";

// The operator's handle at its edges: crash drills at each commit point of an operator request
// (and of the team log's receipt), a dynamic member started by the operator, and hydration's
// StoreCorruptError. Every test ends with the team's replay check.

class Crash extends Error {}

const TEAM_ID = "0192c000-0000-7000-8000-000000000001";
const OPERATOR = { issuer: "api", tenant: "local", subject: "operator" };

/** The same database, through a driver that dies once at the first write `at` matches. */
function crashing(
  base: StoreDriver,
  at: (sql: string, params: readonly SqlValue[]) => boolean,
): StoreDriver {
  let crashed = false;
  const wrap = (tx: Tx): Tx => ({
    ...tx,
    run: (sql, params = []) => {
      if (!crashed && at(sql, params)) {
        crashed = true;
        throw new Crash(sql);
      }
      return tx.run(sql, params);
    },
    transaction: (fn) => tx.transaction((inner) => fn(wrap(inner))),
  });
  return {
    ...base,
    transaction: (fn, options) =>
      base.transaction((tx) => fn(wrap(tx)), options),
  };
}

/** Artifacts whose reads of `broken` fail: missing, or the right hash with the wrong length. */
function breakable(): ArtifactStore & {
  readonly broken: Map<string, "missing" | "short">;
} {
  const inner = memoryArtifacts();
  const broken = new Map<string, "missing" | "short">();
  return {
    ...inner,
    broken,
    get: async (sha) => {
      const how = broken.get(sha);
      const got = await inner.get(sha);
      if (how === "missing")
        return err(logError("artifact_missing", `no artifact ${sha}`));
      return how === "short" && got.ok ? ok(got.value.slice(1)) : got;
    },
  };
}

async function world(writer: readonly string[], leadStarts = false) {
  const db = openBunSqlite(":memory:");
  const artifacts = breakable();
  const log = unwrap(await LogStore.open(db, Date.now, artifacts));
  const store = storeOf({ log, artifacts });
  const lead = agent({
    name: "lead",
    model: scriptedModel({
      responses: [
        ...(leadStarts ? [start("c1", "writer", "Go.")] : []),
        say("Ready."),
        say("Done."),
      ],
    }),
    team: [
      agent({
        name: "writer",
        model: scriptedModel({ responses: writer.map((t) => say(t)) }),
      }),
    ],
  });
  const r = await lead.run("Get ready.", { store });
  const entry = memberEntry(lead);
  if (entry === undefined) throw new Error("agent() registers the lead");
  /** A handle through another driver of the same database. */
  const through = async (driver: StoreDriver) =>
    teamHandle({
      log: unwrap(await LogStore.open(driver, Date.now, artifacts)),
      artifacts,
      ref: r.team.ref,
      principal: OPERATOR,
      lead: entry,
    });
  const teamLog = async () => {
    const row = await reading(db, (tx) => teamRow(tx, r.team.ref.id));
    if (row === undefined) throw new Error("no team");
    return knownEvents(unwrap(await log.read(row.team_log_branch_id)));
  };
  return { db, log, artifacts, team: r.team, through, teamLog };
}

const count = (events: readonly { type: string }[], type: string): number =>
  events.filter((e) => e.type === type).length;

describe("operator crash drills", () => {
  test("a crash inside team.start stores none of it; the keyed retry starts once", async () => {
    const w = await world([]);
    const dying = await w.through(
      crashing(w.db, (sql) => sql.includes("INSERT INTO operator_receipts")),
    );
    await expect(
      dying.start("writer", "Draft.", { idempotencyKey: "k" }),
    ).rejects.toThrow(Crash);
    expect(count(await w.teamLog(), "operator_request")).toBe(0);
    const retried = await w.team.start("writer", "Draft.", {
      idempotencyKey: "k",
    });
    expect(retried.status).toBe("started");
    // A second retry (the answer was lost after the commit) replays it.
    expect(
      await w.team.start("writer", "Draft.", { idempotencyKey: "k" }),
    ).toEqual(retried);
    expect(count(await w.teamLog(), "member_started")).toBe(1);
    await assertTeamReplays(w.log, w.team.ref.id);
  });

  test("a crash inside team.send stores none of it; the keyed retry sends once", async () => {
    const w = await world([]);
    const started = await w.team.start("writer", "Draft.");
    if (started.status !== "started") throw new Error("started");
    const dying = await w.through(
      crashing(w.db, (sql) => sql.includes("INSERT INTO mail")),
    );
    await expect(
      dying.send(started.member, "Short.", { idempotencyKey: "s" }),
    ).rejects.toThrow(Crash);
    const sent = await w.team.send(started.member, "Short.", {
      idempotencyKey: "s",
    });
    expect(sent.status).toBe("sent");
    const messages = (await w.teamLog()).filter(
      (e) => e.type === "message_sent" && e.data.envelope.kind === "message",
    );
    expect(messages).toHaveLength(1);
    await assertTeamReplays(w.log, w.team.ref.id);
  });

  test("a crash inside the team log's receipt leaves the notice pending; the next step takes it once", async () => {
    const w = await world([]);
    await w.team.start("writer", "Draft.");
    // The rebind fails: the member ends at once and notifies its starter, the team log.
    unwrap(
      await materialize(w.log, w.team.ref.id, "writer-1", {
        artifacts: w.artifacts,
        rebind: async () => ({ status: "pin_unavailable" }),
        holder: "test",
        ttlMs: 30_000,
      }),
    );
    const dying = unwrap(
      await LogStore.open(
        crashing(w.db, (sql) =>
          sql.includes("UPDATE mail SET state = 'consumed'"),
        ),
        Date.now,
        w.artifacts,
      ),
    );
    await expect(
      takeTeamLogMail(dying, w.artifacts, w.team.ref.id, undefined),
    ).rejects.toThrow(Crash);
    expect(count(await w.teamLog(), "message_received")).toBe(0);
    await takeTeamLogMail(w.log, w.artifacts, w.team.ref.id, undefined);
    await takeTeamLogMail(w.log, w.artifacts, w.team.ref.id, undefined);
    expect(count(await w.teamLog(), "message_received")).toBe(1);
    await assertTeamReplays(w.log, w.team.ref.id);
  });
});

describe("hydration", () => {
  const big = "x".repeat(20_000);

  test("members() reads a large output from the artifact store", async () => {
    const w = await world([big], true);
    const writer = (await w.team.members()).find((m) => m.name === "writer-1");
    expect(writer?.result?.status === "completed" && writer.result.output).toBe(
      big,
    );
    await assertTeamReplays(w.log, w.team.ref.id);
  });

  for (const how of ["missing", "short"] as const)
    test(`a ${how} output artifact throws StoreCorruptError`, async () => {
      const w = await world([big], true);
      const [row] = z
        .array(z.object({ result: z.instanceof(Uint8Array) }))
        .parse(
          await query(
            w.db,
            "SELECT result FROM team_members WHERE name = 'writer-1'",
            [],
          ),
        );
      const stored = StoredMemberResult.parse(
        z.json().parse(JSON.parse(new TextDecoder().decode(row?.result))),
      );
      if (stored.status !== "completed" || !("ref" in stored.output))
        throw new Error("a large output is a ref");
      const { ref } = stored.output;
      w.artifacts.broken.set(ref.sha256, how);
      const thrown = await w.team.members().catch((e: unknown) => e);
      expect(thrown).toBeInstanceOf(StoreCorruptError);
      expect(thrown instanceof StoreCorruptError && thrown.code).toBe(
        how === "missing" ? "artifact_missing" : "artifact_corrupt",
      );
      expect(thrown instanceof StoreCorruptError && thrown.ref).toEqual(ref);
      // The audit feed returns results as stored, and never throws.
      const feed: Json[] = [];
      for await (const item of w.team.events()) feed.push(item.kind);
      expect(feed.length).toBeGreaterThan(0);
      await assertTeamReplays(w.log, w.team.ref.id);
    });
});

test("an output artifact that isn't UTF-8 throws StoreCorruptError", async () => {
  const artifacts = memoryArtifacts();
  const bytes = new Uint8Array([0xff, 0xfe]);
  const ref = {
    sha256: await artifacts.put(bytes),
    bytes: bytes.length,
    media_type: "text/plain",
  };
  const member = {
    tenant: "local",
    team: TEAM_ID,
    name: "writer-1",
    generation: 1,
  };
  const stored = StoredMemberResult.parse({
    member,
    status: "completed",
    output: { ref },
  });
  await expect(hydrated(stored, artifacts)).rejects.toThrow(StoreCorruptError);
});

describe("an operator's dynamic member", () => {
  test("team.start with a dynamic agent's fields, and its refusal's detail replayed", async () => {
    const db = openBunSqlite(":memory:");
    const artifacts = memoryArtifacts();
    const log = unwrap(await LogStore.open(db, Date.now, artifacts));
    const store = storeOf({ log, artifacts });
    const lead = agent({
      name: "lead",
      model: scriptedModel({ responses: [say("Ready.")] }),
      team: [specialist()],
    });
    const { team } = await lead.run("Get ready.", { store });
    const started = await team.start("specialist", "Is INV-1002 paid?", {
      label: "checker",
      instructions: "Answer yes or no.",
      tools: ["invoice_status"],
      model: "strong",
    });
    expect(started.status).toBe("started");
    const row = await reading(db, (tx) => teamRow(tx, team.ref.id));
    if (row === undefined) throw new Error("no team");
    const events = knownEvents(unwrap(await log.read(row.team_log_branch_id)));
    const member = events.find((e) => e.type === "member_started");
    expect(member?.type === "member_started" && member.data.define).toEqual({
      instructions: "Answer yes or no.",
      tools: ["invoice_status"],
      model: "strong",
    });
    expect(member?.type === "member_started" && member.data.label).toBe(
      "checker",
    );
    const listed = await team.members();
    expect(listed.find((m) => m.name === "specialist-1")?.label).toBe(
      "checker",
    );
    const refused = await team.start("specialist", "Push.", {
      tools: ["git_push"],
      idempotencyKey: "bad",
    });
    expect(refused).toEqual({
      status: "refused",
      code: "invalid_definition",
      detail: {
        field: "tools",
        reason: "not_allowed",
        allowed: ["invoice_status", "read_notes"],
      },
    });
    expect(
      await team.start("specialist", "Push.", {
        tools: ["git_push"],
        idempotencyKey: "bad",
      }),
    ).toEqual(refused);
    await assertTeamReplays(log, team.ref.id);
  });
});
