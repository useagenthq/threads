import { expect } from "bun:test";
import { z } from "zod";
import { openStore, storeConnection } from "../../src/agent/sqlite";
import { sha256Hex } from "../../src/hash";
import { canonicalize, type ThreadId } from "../../src/log";
import { unwrap } from "../store/helpers";
import { configHash, type Store } from "./usage-kit";

// Rewrites a stored log the way a writer (or an import) would have produced it: every line
// canonical, re-chained, the head moved, and thread_started's config_hash recomputed when its
// config changed. The result verifies, so a test gets a real log with the shape it needs.

export type Line = Record<string, z.core.util.JSONType>;
const Line = z.record(z.string(), z.json());

/** A JSON object of a log line. */
export function obj(value: z.core.util.JSONType | undefined): Line {
  return Line.parse(value);
}

/** An edit that replaces thread_started's data with `edit(data)`. */
export function editStarted(
  edit: (data: Line) => Line,
): (lines: readonly Line[]) => readonly Line[] {
  return ([started, ...rest]) =>
    started === undefined
      ? []
      : [{ ...started, data: edit(obj(started["data"])) }, ...rest];
}
const Rows = z.array(
  z.object({ seq: z.number(), line: z.instanceof(Uint8Array) }),
);
const Header = z.array(z.object({ header_line: z.instanceof(Uint8Array) }));

/** The pin config_hash covers: thread_started's data but its hash and its parent link. */
function config(line: Line | undefined): Line {
  const { config_hash: _hash, parent: _parent, ...rest } = obj(line?.["data"]);
  return rest;
}

/** Replaces `thread`'s main-branch lines with `edit(lines)` (a prefix of the same length or shorter). */
export async function rewriteLog(
  store: Store,
  thread: ThreadId,
  edit: (lines: readonly Line[]) => readonly Line[],
): Promise<void> {
  const { db } = await storeConnection(store);
  const branch = unwrap((await openStore(store)).log.mainBranch(thread));
  const rows = Rows.parse(
    db.all("SELECT seq, line FROM events WHERE branch_id = ? ORDER BY seq", [
      branch,
    ]),
  );
  const [header] = Header.parse(
    db.all("SELECT header_line FROM branches WHERE branch_id = ?", [branch]),
  );
  const text = new TextDecoder();
  const before = rows.map((r) => Line.parse(JSON.parse(text.decode(r.line))));
  const after = edit(structuredClone(before)).map((line) => ({ ...line }));
  const [started] = after;
  if (
    started !== undefined &&
    configHash(config(started)) !== configHash(config(before[0]))
  ) {
    // Proves the formula on the stored pin before re-hashing the edited one.
    expect(obj(before[0]?.["data"])["config_hash"]).toBe(
      configHash(config(before[0])),
    );
    started["data"] = {
      ...obj(started["data"]),
      config_hash: configHash(config(started)),
    };
  }
  let prev = sha256Hex(header?.header_line ?? new Uint8Array());
  after.forEach((line, i) => {
    const bytes = new TextEncoder().encode(
      unwrap(canonicalize({ ...line, prev_hash: prev })),
    );
    db.run("UPDATE events SET line = ? WHERE branch_id = ? AND seq = ?", [
      bytes,
      branch,
      rows[i]?.seq ?? 0,
    ]);
    prev = sha256Hex(bytes);
  });
  db.run("DELETE FROM events WHERE branch_id = ? AND seq > ?", [
    branch,
    after.length,
  ]);
  db.run(
    "UPDATE branches SET head_seq = ?, head_hash = ? WHERE branch_id = ?",
    [after.length, prev, branch],
  );
}
