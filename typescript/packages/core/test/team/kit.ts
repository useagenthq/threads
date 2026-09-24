import { readFileSync } from "node:fs";
import { join } from "node:path";
import { z } from "zod";
import { sha256Hex } from "../../src/hash";
import { type BranchId, canonicalize, type TeamId } from "../../src/log";
import type { SqliteDriver } from "../../src/store";
import { importSegments } from "../../src/store/import";
import type { LogStore } from "../../src/store/store";
import { rebuildTeamIndex } from "../../src/team/rebuild";
import { type VerifiedLog, verifyExport } from "../../src/verify";
import { fixture, unwrap } from "../store/helpers";

// Shared by the team tests: reading the team cases' logs into a store, and the index rows as the
// `team` conformance kind compares them.

const SPEC: string = join(import.meta.dir, "../../../../../spec/conformance");
/** The team cases of the corpus, whose logs these tests build on. */
export const CASES: string = join(SPEC, "cases");
/** Cases still staged for another lane. */
export const STAGED: string = join(SPEC, "staged");

/** A team case's log bytes. */
export function caseLog(name: string, label: string): Uint8Array {
  return new Uint8Array(
    readFileSync(join(CASES, name, "logs", `${label}.jsonl`)),
  );
}

type Line = Record<string, unknown>;
const utf8Out = new TextEncoder();

/**
 * An export with each event line replaced by `edit(line)`, re-chained and headed the way a
 * writer would have written it, so a test gets a real log of the shape it needs.
 */
export function relinked(
  bytes: Uint8Array,
  edit: (line: Line) => Line,
): Uint8Array {
  const [header = "", ...rest] = utf8.decode(bytes).split("\n");
  const events = rest.filter((l) => l !== "" && !l.includes('"threads.head"'));
  let prev = sha256Hex(utf8Out.encode(header));
  const lines = [header];
  let last: Line = {};
  for (const text of events) {
    const line = { ...edit(Row.parse(JSON.parse(text))), prev_hash: prev };
    const out = unwrap(canonicalize(z.json().parse(line)));
    lines.push(out);
    prev = sha256Hex(utf8Out.encode(out));
    last = line;
  }
  const head = {
    format: "threads.head",
    format_version: 1,
    branch_id: last["branch_id"],
    seq: last["seq"],
    hash: prev,
  };
  lines.push(unwrap(canonicalize(z.json().parse(head))));
  return utf8Out.encode(`${lines.join("\n")}\n`);
}

/**
 * The export of a repair fork `child` of the branch `bytes` exports, at its line `atSeq`: the
 * parent's lines through the fork point, the child's header, its fork event and its head.
 */
export function forkedAt(
  bytes: Uint8Array,
  atSeq: number,
  child: string,
): Uint8Array {
  const [header = "", ...rest] = utf8.decode(bytes).split("\n");
  const parent = Row.parse(JSON.parse(header));
  const at = rest[atSeq - 1] ?? "";
  const own = unwrap(
    canonicalize(z.json().parse({ ...parent, branch_id: child })),
  );
  const fork = unwrap(
    canonicalize(
      z.json().parse({
        actor: { kind: "host" },
        branch_id: child,
        critical: true,
        data: {
          at_hash: sha256Hex(utf8Out.encode(at)),
          parent_branch_id: parent["branch_id"],
          reason: "repair",
        },
        epoch: 2,
        event_id: "0192e00f-0000-7000-8000-000000000001",
        prev_hash: sha256Hex(utf8Out.encode(own)),
        seq: atSeq + 1,
        thread_id: parent["thread_id"],
        time: 1_790_000_099_000,
        type: "fork",
        type_version: 1,
      }),
    ),
  );
  const head = unwrap(
    canonicalize(
      z.json().parse({
        format: "threads.head",
        format_version: 1,
        branch_id: child,
        seq: atSeq + 1,
        hash: sha256Hex(utf8Out.encode(fork)),
      }),
    ),
  );
  const lines = [header, ...rest.slice(0, atSeq), own, fork, head];
  return utf8Out.encode(`${lines.join("\n")}\n`);
}

/** Verifies an export; a test fails loudly when it doesn't. */
export function verified(bytes: Uint8Array): VerifiedLog {
  return unwrap(verifyExport(bytes));
}

/**
 * Stores verified logs byte for byte, as import does, without replaying their model requests:
 * the team cases ship no artifacts.
 */
export function storeLogs(store: LogStore, logs: readonly VerifiedLog[]): void {
  for (const log of logs)
    unwrap(
      importSegments(store.driver, log, {
        tenantId: store.tenant,
        droppedRef: null,
      }),
    );
}

/** The team cases' tenant: a lead's team is indexed under its principal's tenant. */
export const TENANT = "acme";

/** A team case, by label, stored and indexed as its appends would have left it. */
export type Team = {
  readonly store: LogStore;
  readonly db: SqliteDriver;
  readonly team: TeamId;
  readonly logs: ReadonlyMap<string, VerifiedLog>;
};

/** A store holding `logs` (label to bytes), with the team index rebuilt from them. */
export function teamStore(
  logs: ReadonlyMap<string, Uint8Array>,
  tenant: string = TENANT,
  driver?: SqliteDriver,
): Team {
  const { store, db } = fixture(tenant, driver);
  const read = new Map(
    [...logs].map(([label, bytes]) => [label, verified(bytes)]),
  );
  storeLogs(store, [...read.values()]);
  const team = teamOf([...read.values()]);
  for (const each of teamsOf([...read.values()]))
    unwrap(rebuildTeamIndex(store, each));
  return { store, db, team, logs: read };
}

/** A team case's logs, each optionally edited and re-chained. */
export function caseLogs(
  name: string,
  labels: readonly string[],
  edit: (
    label: string,
    line: Record<string, unknown>,
  ) => Record<string, unknown> = (_, line) => line,
): ReadonlyMap<string, Uint8Array> {
  return new Map(
    labels.map((label) => [
      label,
      relinked(caseLog(name, label), (line) => edit(label, line)),
    ]),
  );
}

/** Every team a lead among the logs leads, in log order. */
export function teamsOf(logs: readonly VerifiedLog[]): readonly TeamId[] {
  return logs.flatMap((log) =>
    log.events.flatMap((line) =>
      line.kind === "event" &&
      line.event.type === "thread_started" &&
      line.event.data.team !== undefined
        ? [line.event.data.team.id]
        : [],
    ),
  );
}

/** The first lead's team id: the team its thread_started names. */
export function teamOf(logs: readonly VerifiedLog[]): TeamId {
  const team = teamsOf(logs)[0];
  if (team === undefined) throw new Error("no lead among the logs");
  return team;
}

const Row = z.record(z.string(), z.unknown());
const JSON_COLUMNS: ReadonlySet<string> = new Set([
  "envelope",
  "provenance",
  "result",
]);
const CLAIMS: ReadonlySet<string> = new Set([
  "claim_token",
  "claim_expires_at",
]);
const utf8 = new TextDecoder();

function rows(
  db: SqliteDriver,
  sql: string,
  params: readonly string[],
): unknown[] {
  return db.all(sql, params).map((raw) => {
    const row = Row.parse(raw);
    return Object.fromEntries(
      Object.entries(row)
        .filter(([key]) => !CLAIMS.has(key))
        .map(([key, value]) => [
          key,
          JSON_COLUMNS.has(key) && value instanceof Uint8Array
            ? JSON.parse(utf8.decode(value))
            : value,
        ]),
    );
  });
}

/**
 * A team's index rows as `$defs/TeamIndex` of case.schema.json has them: each table sorted by
 * its primary key, JSON columns parsed, mail's claim columns left out, the feed as its
 * (branch_id, seq) rows, and the wake rows of the team's branches.
 */
export function teamIndexRows(
  db: SqliteDriver,
  teams: readonly TeamId[],
  branches: readonly BranchId[],
): Record<string, unknown[]> {
  const inTeams = `team_id IN (${teams.map(() => "?").join(", ")})`;
  const by = (table: string, order: string): unknown[] =>
    rows(
      db,
      `SELECT * FROM ${table} WHERE ${inTeams} ORDER BY ${order}`,
      teams,
    );
  const marks = branches.map(() => "?").join(", ");
  return {
    teams: by("teams", "team_id"),
    team_members: by("team_members", "team_id, name, generation"),
    mail: by("mail", "mail_id"),
    asks: by("asks", "ask_id"),
    monitors: by("monitors", "monitor_id"),
    operator_receipts: by(
      "operator_receipts",
      "tenant_id, team_id, op, idempotency_key",
    ),
    team_feed: rows(
      db,
      `SELECT team_id, branch_id, seq FROM team_feed WHERE ${inTeams} ORDER BY team_id, branch_id, seq`,
      teams,
    ),
    pending_wakes: rows(
      db,
      `SELECT * FROM pending_wakes WHERE branch_id IN (${marks}) ORDER BY branch_id, child_thread_id`,
      branches,
    ),
  };
}
